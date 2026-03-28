import ast
from collections import defaultdict


class CallGraphVisitor(ast.NodeVisitor):
    def __init__(self):
        self.current_function = None
        self.calls = defaultdict(set)

    def visit_FunctionDef(self, node):
        previous = self.current_function
        self.current_function = node.name
        self.generic_visit(node)
        self.current_function = previous

    def visit_Call(self, node):
        if self.current_function is None:
            return

        if isinstance(node.func, ast.Name):
            self.calls[self.current_function].add(node.func.id)

        self.generic_visit(node)


def build_call_graph(chunks: list[dict]) -> dict:
    """
    Build a conservative call graph using AST.
    Only includes Python files.
    """

    call_graph = defaultdict(set)

    for chunk in chunks:
        path = chunk["source"]
        text = chunk["text"]

        if not path.endswith(".py"):
            continue

        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        visitor = CallGraphVisitor()
        visitor.visit(tree)

        for caller, callees in visitor.calls.items():
            for callee in callees:
                call_graph[caller].add({
                    "calls": callee,
                    "defined_in": path
                })

    # Make it JSON-friendly
    return {
        caller: list(callees)
        for caller, callees in call_graph.items()
    }
