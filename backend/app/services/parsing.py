"""
Shared tree-sitter parsing helpers — "parse ONCE, use twice" (handoff §3).

Both the chunker (services/chunking.py) and the graph builder
(services/graph_builder.py) need the syntax tree of a file. Parsing is cheap
but not free, so this module is the single place that:

  1. maps file extensions → tree-sitter grammars,
  2. parses source text into a tree (with caching of parser objects),
  3. extracts *facts* from the tree: definitions, imports, calls, inheritance.

Everything downstream consumes plain dicts, so no other module needs to know
tree-sitter's API. This is the "breadth tier" of the tiered-precision plan
(~80% accuracy everywhere); an LSP/Kythe precision tier can override these
facts for the top languages later without touching callers.

Language support tiers (documented, deliberate):
  full   (defs + imports + calls + inheritance): python, javascript, typescript, tsx
  defs   (definitions + imports only):           go, rust
  chunks (no structure, line chunking only):     everything else
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- File filtering (previously lived in chunking.py) -----------------------

IGNORED_DIRS = {
    ".git", ".github", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", "coverage", "out", "target",
}

IGNORED_EXTENSIONS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".mp4", ".mp3", ".wav",
    ".lock", ".csv", ".db", ".sqlite", ".parquet",
}

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".cpp": "cpp",
    ".go": "go",
    ".rs": "rust",
}

# Languages with full structural extraction (calls resolved by the graph builder).
FULL_SUPPORT = {"python", "javascript", "typescript", "tsx"}
# Definitions + imports only.
DEFS_SUPPORT = {"go", "rust", "cpp"}

_parsers: Dict[str, Any] = {}  # grammar name -> cached parser


def language_for(file_path: str) -> Optional[str]:
    """Return the tree-sitter grammar name for a file, or None."""
    _, ext = os.path.splitext(file_path)
    return LANGUAGE_MAP.get(ext.lower())


def is_processable_file(file_path: str) -> bool:
    """Skip vendored/binary/non-code files. Shared by chunker + graph builder."""
    parts = set(file_path.replace("\\", "/").split("/"))
    if parts.intersection(IGNORED_DIRS):
        return False
    return language_for(file_path) is not None


def parse(text: str, lang_name: str):
    """Parse source text into a tree-sitter tree. Returns None on any failure
    (per-file parse isolation — one bad file must never poison a pipeline)."""
    try:
        if lang_name not in _parsers:
            try:
                # Python <= 3.12 (pinned wheels)
                from tree_sitter_languages import get_parser
            except ImportError:
                # Maintained successor, supports Python 3.13+ — same API
                from tree_sitter_language_pack import get_parser
            _parsers[lang_name] = get_parser(lang_name)
        return _parsers[lang_name].parse(bytes(text, "utf8"))
    except Exception as e:
        logger.warning(f"tree-sitter parse failed for lang={lang_name}: {e}")
        return None


def _node_text(node, source_bytes: bytes) -> str:
    # tree-sitter offsets are BYTE offsets into the UTF-8 source. Slicing the
    # decoded str with them shifts everything after any multi-byte character
    # (em-dashes, accents, CJK), so all slicing goes through the bytes.
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_line(node, source_bytes: bytes) -> str:
    return _node_text(node, source_bytes).split("\n", 1)[0].strip()


# --- Definitions -------------------------------------------------------------

def extract_definitions(tree, text: str, lang: str) -> List[Dict[str, Any]]:
    """
    Walk the tree and return every class/function/method definition:
      {kind, name, parent (enclosing class name or None), bases [inheritance],
       signature, start_line, end_line, start_byte, end_byte}
    Lines are 1-based (what editors and humans use).
    """
    if tree is None:
        return []
    text = text.encode("utf-8")  # slice bytes, not str — offsets are bytes
    defs: List[Dict[str, Any]] = []

    def add(kind, name, node, parent=None, bases=None):
        defs.append({
            "kind": kind,
            "name": name,
            "parent": parent,
            "bases": bases or [],
            "signature": _first_line(node, text).rstrip("{:").strip(),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
        })

    def walk(node, parent_class=None):
        for child in node.children:
            actual = child
            if lang == "python" and child.type == "decorated_definition":
                actual = child.child_by_field_name("definition") or child

            if lang == "python":
                if actual.type == "class_definition":
                    name_node = actual.child_by_field_name("name")
                    name = _node_text(name_node, text) if name_node else "<anon>"
                    bases = _python_bases(actual, text)
                    add("class", name, child, parent_class, bases)
                    body = actual.child_by_field_name("body")
                    if body:
                        walk(body, parent_class=name)
                    continue
                if actual.type == "function_definition":
                    name_node = actual.child_by_field_name("name")
                    name = _node_text(name_node, text) if name_node else "<anon>"
                    kind = "method" if parent_class else "function"
                    add(kind, name, child, parent_class)
                    continue

            elif lang in ("javascript", "typescript", "tsx"):
                if actual.type in ("class_declaration", "abstract_class_declaration"):
                    name_node = actual.child_by_field_name("name")
                    name = _node_text(name_node, text) if name_node else "<anon>"
                    add("class", name, actual, parent_class, _js_bases(actual, text))
                    body = actual.child_by_field_name("body")
                    if body:
                        walk(body, parent_class=name)
                    continue
                if actual.type in ("function_declaration", "generator_function_declaration"):
                    name_node = actual.child_by_field_name("name")
                    add("function", _node_text(name_node, text) if name_node else "<anon>",
                        actual, parent_class)
                    continue
                if actual.type == "method_definition":
                    name_node = actual.child_by_field_name("name")
                    add("method", _node_text(name_node, text) if name_node else "<anon>",
                        actual, parent_class)
                    continue
                if actual.type in ("lexical_declaration", "variable_declaration"):
                    # const foo = () => {...}  /  const foo = function() {...}
                    for decl in actual.children:
                        if decl.type == "variable_declarator":
                            value = decl.child_by_field_name("value")
                            if value is not None and value.type in ("arrow_function", "function", "function_expression"):
                                name_node = decl.child_by_field_name("name")
                                add("function",
                                    _node_text(name_node, text) if name_node else "<anon>",
                                    actual, parent_class)
                    continue

            elif lang == "go":
                if actual.type in ("function_declaration", "method_declaration"):
                    name_node = actual.child_by_field_name("name")
                    add("function", _node_text(name_node, text) if name_node else "<anon>", actual)
                    continue
                if actual.type == "type_declaration":
                    for spec in actual.children:
                        if spec.type == "type_spec":
                            name_node = spec.child_by_field_name("name")
                            if name_node is not None:
                                add("class", _node_text(name_node, text), actual)
                    continue

            elif lang == "rust":
                if actual.type == "function_item":
                    name_node = actual.child_by_field_name("name")
                    add("method" if parent_class else "function",
                        _node_text(name_node, text) if name_node else "<anon>",
                        actual, parent_class)
                    continue
                if actual.type in ("struct_item", "enum_item", "trait_item"):
                    name_node = actual.child_by_field_name("name")
                    add("class", _node_text(name_node, text) if name_node else "<anon>", actual)
                    continue
                if actual.type == "impl_item":
                    type_node = actual.child_by_field_name("type")
                    impl_name = _node_text(type_node, text) if type_node else None
                    body = actual.child_by_field_name("body")
                    if body:
                        walk(body, parent_class=impl_name)
                    continue

            elif lang == "cpp":
                if actual.type == "function_definition":
                    decl = actual.child_by_field_name("declarator")
                    name = _first_line(decl, text).split("(")[0].strip() if decl else "<anon>"
                    add("function", name.split("::")[-1] or "<anon>", actual, parent_class)
                    continue
                if actual.type in ("class_specifier", "struct_specifier"):
                    name_node = actual.child_by_field_name("name")
                    if name_node is not None:
                        name = _node_text(name_node, text)
                        add("class", name, actual, parent_class)
                        body = actual.child_by_field_name("body")
                        if body:
                            walk(body, parent_class=name)
                    continue

            # Recurse into containers we didn't classify (e.g. blocks, namespaces)
            if child.type not in ("string", "comment") and child.child_count > 0 and lang not in FULL_SUPPORT:
                continue  # keep the defs tier shallow for partially-supported langs

    walk(tree.root_node)
    return defs


def _python_bases(class_node, text: bytes) -> List[str]:
    """`class Billing(Base, mixins.Audited):` -> ['Base', 'mixins.Audited']"""
    supers = class_node.child_by_field_name("superclasses")
    if supers is None:
        return []
    bases = []
    for child in supers.children:
        if child.type in ("identifier", "attribute"):
            bases.append(_node_text(child, text))
    return bases


def _js_bases(class_node, text: bytes) -> List[str]:
    """`class Admin extends User` -> ['User']"""
    for child in class_node.children:
        if child.type == "class_heritage":
            return [
                _node_text(c, text)
                for c in child.children
                if c.type in ("identifier", "member_expression")
            ]
    return []


# --- Imports -----------------------------------------------------------------

def extract_imports(tree, text: str, lang: str) -> List[Dict[str, Any]]:
    """
    Return file-level imports:
      {module, names: [(imported_name, local_alias)], alias, line}
    Examples (python):
      import stripe            -> {module: 'stripe', names: [], alias: 'stripe'}
      import numpy as np       -> {module: 'numpy', names: [], alias: 'np'}
      from app.db import get_conn as gc
                               -> {module: 'app.db', names: [('get_conn','gc')], alias: None}
    """
    if tree is None:
        return []
    text = text.encode("utf-8")  # slice bytes, not str — offsets are bytes
    imports: List[Dict[str, Any]] = []

    for node in tree.root_node.children:
        try:
            if lang == "python":
                if node.type == "import_statement":
                    for child in node.children:
                        if child.type == "dotted_name":
                            mod = _node_text(child, text)
                            imports.append({"module": mod, "names": [], "alias": mod.split(".")[0], "line": node.start_point[0] + 1})
                        elif child.type == "aliased_import":
                            name_node = child.child_by_field_name("name")
                            alias_node = child.child_by_field_name("alias")
                            imports.append({
                                "module": _node_text(name_node, text) if name_node else "",
                                "names": [],
                                "alias": _node_text(alias_node, text) if alias_node else None,
                                "line": node.start_point[0] + 1,
                            })
                elif node.type == "import_from_statement":
                    mod_node = node.child_by_field_name("module_name")
                    module = _node_text(mod_node, text) if mod_node else ""
                    names: List[Tuple[str, str]] = []
                    seen_import_kw = False
                    for child in node.children:
                        if child.type == "import":
                            seen_import_kw = True
                            continue
                        if not seen_import_kw:
                            continue
                        if child.type == "dotted_name":
                            n = _node_text(child, text)
                            names.append((n, n))
                        elif child.type == "aliased_import":
                            name_node = child.child_by_field_name("name")
                            alias_node = child.child_by_field_name("alias")
                            n = _node_text(name_node, text) if name_node else ""
                            names.append((n, _node_text(alias_node, text) if alias_node else n))
                        elif child.type == "wildcard_import":
                            names.append(("*", "*"))
                    imports.append({"module": module, "names": names, "alias": None, "line": node.start_point[0] + 1})

            elif lang in ("javascript", "typescript", "tsx"):
                if node.type == "import_statement":
                    source = node.child_by_field_name("source")
                    module = _node_text(source, text).strip("'\"") if source else ""
                    names: List[Tuple[str, str]] = []
                    for child in node.children:
                        if child.type == "import_clause":
                            for c in child.children:
                                if c.type == "identifier":  # default import
                                    names.append((_node_text(c, text), _node_text(c, text)))
                                elif c.type == "named_imports":
                                    for spec in c.children:
                                        if spec.type == "import_specifier":
                                            name_node = spec.child_by_field_name("name")
                                            alias_node = spec.child_by_field_name("alias")
                                            n = _node_text(name_node, text) if name_node else ""
                                            names.append((n, _node_text(alias_node, text) if alias_node else n))
                                elif c.type == "namespace_import":
                                    for cc in c.children:
                                        if cc.type == "identifier":
                                            names.append(("*", _node_text(cc, text)))
                    imports.append({"module": module, "names": names, "alias": None, "line": node.start_point[0] + 1})

            elif lang == "go":
                if node.type == "import_declaration":
                    for spec in node.children:
                        specs = [spec] if spec.type == "import_spec" else [
                            s for s in spec.children if s.type == "import_spec"
                        ]
                        for s in specs:
                            path_node = s.child_by_field_name("path")
                            if path_node is not None:
                                mod = _node_text(path_node, text).strip('"')
                                imports.append({"module": mod, "names": [], "alias": mod.split("/")[-1], "line": s.start_point[0] + 1})

            elif lang == "rust":
                if node.type == "use_declaration":
                    arg = node.child_by_field_name("argument")
                    if arg is not None:
                        imports.append({"module": _node_text(arg, text), "names": [], "alias": None, "line": node.start_point[0] + 1})
        except Exception as e:  # never let one weird import break the file
            logger.debug(f"import extraction skipped a node: {e}")

    return imports


# --- Calls -------------------------------------------------------------------

def extract_calls(tree, text: str, lang: str,
                  start_byte: Optional[int] = None,
                  end_byte: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Return call sites, optionally restricted to a byte range (a chunk or a def):
      {name, receiver, line}
    `foo(x)`        -> {name: 'foo', receiver: None}
    `svc.charge(x)` -> {name: 'charge', receiver: 'svc'}
    Only meaningful for FULL_SUPPORT languages; returns [] otherwise.
    """
    if tree is None or lang not in FULL_SUPPORT:
        return []
    text = text.encode("utf-8")  # slice bytes, not str — offsets are bytes
    call_type = "call" if lang == "python" else "call_expression"
    calls: List[Dict[str, Any]] = []

    def walk(node):
        if start_byte is not None and node.end_byte < start_byte:
            return
        if end_byte is not None and node.start_byte > end_byte:
            return
        if node.type == call_type:
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    calls.append({"name": _node_text(fn, text), "receiver": None,
                                  "line": node.start_point[0] + 1})
                elif fn.type in ("attribute", "member_expression"):
                    obj = fn.child_by_field_name("object")
                    attr = (fn.child_by_field_name("attribute")
                            or fn.child_by_field_name("property"))
                    if attr is not None:
                        calls.append({
                            "name": _node_text(attr, text),
                            "receiver": _node_text(obj, text) if obj is not None else None,
                            "line": node.start_point[0] + 1,
                        })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return calls
