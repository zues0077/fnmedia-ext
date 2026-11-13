"""全库体检：AST 级别的结构性检查（不依赖第三方 linter）。"""
import ast
import io
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(ROOT, "proxy")
SKIP_DIRS = {"_vendor", "__pycache__"}


def py_files():
    for base, dirs, files in os.walk(PROXY):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def src(p):
    return io.open(p, encoding="utf-8").read()


def main():
    files = sorted(py_files())
    print("Python 文件数（不含 _vendor）:", len(files))

    dup_defs = defaultdict(list)          # (module, name) -> [lineno]
    dup_imports = defaultdict(list)
    big_funcs = []
    async_sleep = []
    bare_except = []
    silent_except = []
    todos = []
    unused_imports = []
    hard_paths = []
    total_lines = 0
    long_files = []

    for p in files:
        text = src(p)
        total_lines += text.count("\n") + 1
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            print("!! 语法错误", rel(p), e)
            continue
        if text.count("\n") > 1200:
            long_files.append((rel(p), text.count("\n")))
        names = Counter()
        imports = Counter()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names[node.name] += 1
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imports[a.name.split(".")[0]] += 1
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports[node.module.split(".")[0]] += 1
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.end_lineno and node.lineno:
                    n = node.end_lineno - node.lineno
                    if n > 150:
                        big_funcs.append((rel(p), node.name, n))
                if isinstance(node, ast.AsyncFunctionDef):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and \
                                getattr(getattr(sub, "func", None), "attr", "") == "sleep" and \
                                getattr(getattr(sub.func, "value", None), "id", "") == "time":
                            async_sleep.append((rel(p), node.name, sub.lineno))
            elif isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    bare_except.append((rel(p), node.lineno))
                elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                    body = node.body
                    if len(body) == 1 and isinstance(body[0], ast.Pass):
                        silent_except.append((rel(p), node.lineno))
        for nm, c in names.items():
            if c > 1:
                dup_defs[(rel(p), nm)] = c
        for m, c in imports.items():
            if c > 1:
                dup_imports[(rel(p), m)] = c
        for m in re.finditer(r"#\s*(TODO|FIXME|HACK|XXX|暂时|临时)", text):
            line = text[:m.start()].count("\n") + 1
            todos.append((rel(p), line, text.splitlines()[line - 1].strip()[:80]))
        for m in re.finditer(r"/(vol[1-9]|usr/trim|etc/systemd)[\w/.-]*", text):
            line = text[:m.start()].count("\n") + 1
            hard_paths.append((rel(p), line, m.group(0)))
        # 粗查未使用导入：名字在正文里只出现一次
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [(a.asname or a.name.split(".")[0]) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [(a.asname or a.name) for a in node.names if a.name != "*"]
            for nm in mods:
                if len(re.findall(r"\b%s\b" % re.escape(nm), text)) <= 1:
                    unused_imports.append((rel(p), nm))

    print("\n== 文件过长（>1200 行）==")
    for f, n in sorted(long_files, key=lambda x: -x[1]):
        print("  %-28s %d 行" % (f, n))

    print("\n== 同名重复定义（每个文件内）==")
    for (f, n), c in sorted(dup_defs.items()):
        print("  %-28s %-28s x%d" % (f, n, c))

    print("\n== 重复 import ==")
    for (f, m), c in sorted(dup_imports.items()):
        print("  %-28s %-22s x%d" % (f, m, c))

    print("\n== 超长函数（>150 行）==")
    for f, n, ln in sorted(big_funcs, key=lambda x: -x[2])[:15]:
        print("  %-28s %-34s %d 行" % (f, n, ln))

    print("\n== async 里用 time.sleep ==")
    for f, n, ln in async_sleep:
        print("  %-28s %s (L%d)" % (f, n, ln))

    print("\n== 裸 except: %d 处；except Exception: pass（静默吞异常）: %d 处 ==" %
          (len(bare_except), len(silent_except)))
    for f, ln in silent_except[:12]:
        print("   silent %-28s L%d" % (f, ln))

    print("\n== TODO/临时标记 %d 处（前 12）==" % len(todos))
    for f, ln, t in todos[:12]:
        print("  %-28s L%-6d %s" % (f, ln, t))

    print("\n== proxy 里写死的系统路径 %d 处（前 10）==" % len(hard_paths))
    for f, ln, p in hard_paths[:10]:
        print("  %-28s L%-6d %s" % (f, ln, p))

    print("\n== 疑似未使用导入 %d 处（前 15）==" % len(unused_imports))
    for f, nm in unused_imports[:15]:
        print("  %-28s %s" % (f, nm))

    print("\n总行数（不含 _vendor）:", total_lines)


if __name__ == "__main__":
    main()
