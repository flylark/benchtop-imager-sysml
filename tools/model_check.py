#!/usr/bin/env python3
"""
model_check.py - automated checks and generated views for a SysML v2 textual model.

Reads a SysML v2 textual model (exported from the modeling tool's text editor) and
produces a report with:

  1. Requirement coverage matrix  - every requirement needs a parent (unless it is a
                                    top-level system requirement), a satisfy, and a verify
  2. Requirement quality lint      - "shall", a measurable value, no vague words
  3. Verification consistency      - does each test state the values in the
                                    requirement it verifies, and a standard method?
  4. Functional allocation         - every function has a part; every part has a job
  5. Workflow checks and timing    - dead ends, unreachable steps, critical path
  6. State machine checks          - reachability, dead-end states, transition test cases
  7. Analysis evaluation           - computes derived attributes (e.g. error budgets)
  8. Diagrams generated from the model (Mermaid): structure, workflow, states,
     requirement tree and per-requirement traceability threads

It understands the subset of SysML v2 textual notation used in this project; it is
not a complete SysML v2 parser. Python 3.8+, standard library only.

Usage:
    python tools/model_check.py BenchtopImager.sysml            # report in ./reports
    python tools/model_check.py BenchtopImager.sysml --out out  # choose output folder

Exit code: 0 = no errors (warnings allowed), 1 = errors found, 2 = could not run.
"""
from __future__ import annotations

import argparse
import ast
import csv
import html
import math
import os
import re
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

# Requirements whose ID starts with one of these are top level and need no parent.
TOP_LEVEL_PREFIXES = ("SR",)

# Standard verification methods (IADT) and the ID prefix expected for each.
METHOD_PREFIX = {"test": "VT", "analysis": "VA", "inspection": "VI", "demonstration": "VD"}

# Words that usually make a requirement ambiguous or unverifiable.
VAGUE_WORDS = [
    "sharp", "fast", "quick", "quickly", "easy", "easily", "user-friendly", "appropriate",
    "adequate", "sufficient", "approximately", "as needed", "etc", "and/or", "robust",
    "high quality", "state-of-the-art", "minimize", "maximize", "optimal", "reasonable",
    "flexible", "efficient", "if possible", "as appropriate",
]

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

NAME = r"(?:'[^']+'|[A-Za-z_]\w*)"
QNAME = rf"{NAME}(?:::{NAME})*"
CHAIN = rf"{NAME}(?:\.{NAME})*"


def unq(name):
    """Strip SysML quotes from a name: 'objective' -> objective."""
    if name and len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1]
    return name


def segs(text):
    """Split a qualified name or feature chain into plain segments."""
    return [unq(p) for p in re.findall(NAME, text or "")]


def last_seg(qname):
    s = segs(qname)
    return s[-1] if s else None


def norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def fmt_num(x):
    if x is None:
        return "-"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4g}" if abs(x) >= 0.01 else f"{x:.3g}"


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


class ParseError(Exception):
    pass


class EvalError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Model data
# --------------------------------------------------------------------------------------

@dataclass
class Stmt:
    header: str
    body: str | None


@dataclass
class Requirement:
    rid: str
    name: str
    doc: str


@dataclass
class PartDef:
    name: str
    doc: str = ""
    parts: dict = field(default_factory=dict)        # name -> type
    ports: dict = field(default_factory=dict)        # name -> (type, conjugated)
    attrs: dict = field(default_factory=dict)        # name -> (type, expr or None)
    connections: list = field(default_factory=list)  # (end_a, end_b)
    exhibits: dict = field(default_factory=dict)     # name -> state def


@dataclass
class Node:
    name: str
    kind: str | None = None      # start, done, action, fork, join, merge, decide
    type: str | None = None
    duration: float | None = None


@dataclass
class ActionDef:
    name: str
    doc: str = ""
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)


@dataclass
class Transition:
    name: str
    src: str
    trigger: str | None
    guard: str | None
    tgt: str


@dataclass
class State:
    name: str
    parent: "State | None" = None
    children: dict = field(default_factory=dict)
    initial: str | None = None
    transitions: list = field(default_factory=list)
    do_ref: str | None = None
    actions: list = field(default_factory=list)   # names of actions declared in the state
    doc: str = ""


@dataclass
class StateDef:
    name: str
    root: State
    doc: str = ""


@dataclass
class Verification:
    vid: str
    name: str
    doc: str
    subject_name: str | None
    subject_type: str | None
    verifies: list


@dataclass
class Calc:
    name: str
    params: list
    expr: str


@dataclass
class Finding:
    level: str      # ERROR, WARNING
    check: str
    message: str


class Model:
    def __init__(self):
        self.requirements = {}   # usage name -> Requirement
        self.part_defs = {}
        self.port_defs = {}      # name -> list of (direction, item name, item type)
        self.item_defs = set()
        self.attr_defs = set()
        self.usages = {}         # package-level usages: name -> (kind, type)
        self.action_defs = {}
        self.state_defs = {}
        self.verifications = {}
        self.calcs = {}
        self.satisfies = []      # (requirement name, satisfier chain)
        self.derivations = []    # (original, derived)
        self.allocations = []    # (function chain, part chain)
        self.unparsed = []       # (context, statement)
        self.docs = []
        self.root_name = "model"


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------

def clean_doc(raw):
    lines = [re.sub(r"^\s*\*\s?", "", ln) for ln in raw.split("\n")]
    return norm_ws(" ".join(lines))


def preprocess(text, model):
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    text = re.sub(r"\[\$\d+\]\s*", "", text)          # display IDs from the editor

    def keep_doc(m):
        model.docs.append(clean_doc(m.group(1)))
        return f"doc __DOC{len(model.docs) - 1}__;"

    text = re.sub(r"\bdoc\s*/\*(.*?)\*/", keep_doc, text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)  # other block comments
    text = re.sub(r"//[^\n]*", "", text)                # line comments
    return text


def match_brace(s, i):
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    raise ParseError("unbalanced braces in model text")


def statements(body):
    """Split a body into top-level statements: 'header;' or 'header { block }'."""
    out, i, start = [], 0, 0
    while i < len(body):
        c = body[i]
        if c == "{":
            j = match_brace(body, i)
            out.append(Stmt(norm_ws(body[start:i]), body[i + 1:j]))
            i = start = j + 1
        elif c == ";":
            h = norm_ws(body[start:i])
            if h:
                out.append(Stmt(h, None))
            i += 1
            start = i
        elif c == "}":
            raise ParseError("unexpected '}' in model text")
        else:
            i += 1
    tail = norm_ws(body[start:])
    if tail:
        out.append(Stmt(tail, None))
    return out


def doc_ref(model, header):
    m = re.fullmatch(r"doc __DOC(\d+)__", header)
    return model.docs[int(m.group(1))] if m else None


def body_doc(model, body):
    if not body:
        return ""
    for st in statements(body):
        d = doc_ref(model, st.header)
        if d is not None:
            return d
    return ""


def is_import(h):
    return re.match(r"(?:private\s+|public\s+)?import\b", h) is not None


def parse_model(text):
    model = Model()
    clean = preprocess(text, model)
    parse_namespace(model, statements(clean), "")
    return model


def parse_namespace(model, stmts, pkg):
    for st in stmts:
        h = st.header
        if not h or is_import(h) or h.startswith("view ") or h.startswith("doc "):
            continue

        m = re.fullmatch(rf"package ({NAME})", h)
        if m and st.body is not None:
            if not pkg:
                model.root_name = unq(m.group(1))
            parse_namespace(model, statements(st.body), unq(m.group(1)))
            continue

        m = re.fullmatch(rf"requirement\s*(?:<'([^']+)'>)?\s*({NAME})", h)
        if m:
            name = unq(m.group(2))
            model.requirements[name] = Requirement(m.group(1) or name, name,
                                                   body_doc(model, st.body))
            continue

        m = re.fullmatch(rf"part def ({NAME})", h)
        if m:
            model.part_defs[unq(m.group(1))] = parse_part_def(model, unq(m.group(1)), st.body)
            continue

        m = re.fullmatch(rf"port def ({NAME})", h)
        if m:
            items = []
            for s in statements(st.body or ""):
                mi = re.fullmatch(rf"(in|out|inout)\s+item\s+({NAME})\s*:\s*({QNAME})", s.header)
                if mi:
                    items.append((mi.group(1), unq(mi.group(2)), last_seg(mi.group(3))))
                elif not s.header.startswith("doc "):
                    model.unparsed.append((f"port def {unq(m.group(1))}", s.header))
            model.port_defs[unq(m.group(1))] = items
            continue

        m = re.fullmatch(rf"(item|attribute) def ({NAME})", h)
        if m:
            (model.item_defs if m.group(1) == "item" else model.attr_defs).add(unq(m.group(2)))
            continue

        m = re.fullmatch(rf"(part|action|state|item) ({NAME})\s*:\s*({QNAME})", h)
        if m:
            model.usages[unq(m.group(2))] = (m.group(1), last_seg(m.group(3)))
            continue

        m = re.fullmatch(rf"action def ({NAME})", h)
        if m:
            model.action_defs[unq(m.group(1))] = parse_action_def(model, unq(m.group(1)), st.body)
            continue

        m = re.fullmatch(rf"state def ({NAME})", h)
        if m:
            root = State(unq(m.group(1)))
            parse_state_body(model, root, st.body or "")
            model.state_defs[root.name] = StateDef(root.name, root, root.doc)
            continue

        m = re.fullmatch(rf"verification def\s*(?:<'([^']+)'>)?\s*({NAME})", h)
        if m:
            v = parse_verification(model, m.group(1) or unq(m.group(2)), unq(m.group(2)), st.body)
            model.verifications[v.vid] = v
            continue

        m = re.fullmatch(rf"calc def ({NAME})", h)
        if m:
            params, expr = [], None
            for s in statements(st.body or ""):
                mp = re.fullmatch(rf"in\s+({NAME})(?:\s*:\s*{QNAME})?", s.header)
                mr = re.fullmatch(rf"return\s*(?:{NAME}\s*)?(?::\s*{QNAME}\s*)?=\s*(.+)", s.header)
                if mp:
                    params.append(unq(mp.group(1)))
                elif mr:
                    expr = mr.group(1)
            model.calcs[unq(m.group(1))] = Calc(unq(m.group(1)), params, expr)
            continue

        m = re.fullmatch(rf"satisfy\s+(?:requirement\s+)?({CHAIN})\s+by\s+({CHAIN})", h)
        if m:
            model.satisfies.append((segs(m.group(1))[-1], m.group(2)))
            continue

        if re.fullmatch(r"#derivation\s+connection(?:\s+\S+)?", h) and st.body is not None:
            original, derived = None, []
            for s in statements(st.body):
                mo = re.fullmatch(rf"end\s+#original\s+::>\s*({CHAIN})", s.header)
                md = re.fullmatch(rf"end\s+#derive\s+::>\s*({CHAIN})", s.header)
                if mo:
                    original = segs(mo.group(1))[-1]
                elif md:
                    derived.append(segs(md.group(1))[-1])
            for d in derived:
                model.derivations.append((original, d))
            continue

        m = re.fullmatch(rf"(?:allocation\s+(?:{NAME}\s+)?)?allocate\s+({CHAIN})\s+to\s+({CHAIN})", h)
        if m:
            model.allocations.append((m.group(1), m.group(2)))
            continue

        model.unparsed.append((pkg or "model", h))


def parse_part_def(model, name, body):
    pd = PartDef(name)
    for s in statements(body or ""):
        h = s.header
        d = doc_ref(model, h)
        if d is not None:
            pd.doc = d
            continue
        m = re.fullmatch(rf"part ({NAME})\s*:\s*({QNAME})", h)
        if m:
            pd.parts[unq(m.group(1))] = last_seg(m.group(2))
            continue
        m = re.fullmatch(rf"port ({NAME})\s*:\s*(~?)\s*({QNAME})", h)
        if m:
            pd.ports[unq(m.group(1))] = (last_seg(m.group(3)), m.group(2) == "~")
            continue
        m = re.fullmatch(rf"attribute ({NAME})\s*(?::\s*({QNAME}))?\s*(?:=\s*(.+))?", h)
        if m:
            pd.attrs[unq(m.group(1))] = (last_seg(m.group(2)) if m.group(2) else None,
                                         m.group(3).strip() if m.group(3) else None)
            continue
        m = re.fullmatch(rf"(?:connection\s+(?:{NAME}\s+)?)?connect\s+({CHAIN})\s+to\s+({CHAIN})", h)
        if m:
            pd.connections.append((m.group(1), m.group(2)))
            continue
        m = re.fullmatch(rf"exhibit\s+state\s+({NAME})\s*:\s*({QNAME})", h)
        if m:
            pd.exhibits[unq(m.group(1))] = last_seg(m.group(2))
            continue
        if h.startswith("view "):
            continue
        model.unparsed.append((f"part def {name}", h))
    return pd


def duration_of(body):
    if not body:
        return None
    for s in statements(body):
        m = re.fullmatch(r"attribute duration_min\s*(?::\s*\S+)?\s*=\s*([\d.]+)", s.header)
        if m:
            return float(m.group(1))
    return None


def parse_action_def(model, name, body):
    """Successions follow SysML v2 textual rules for the forms used here:
    'then <node decl>' links the previous node to the new one and makes it current;
    'then <name>' links the current node to <name> without changing the current node;
    a declaration without 'then' only makes the node current."""
    ad = ActionDef(name)
    prev = None
    for s in statements(body or ""):
        h = s.header
        d = doc_ref(model, h)
        if d is not None:
            ad.doc = d
            continue
        if h.startswith("view "):
            continue
        if h == "first start":
            ad.nodes["start"] = Node("start", "start")
            prev = "start"
            continue
        m = re.fullmatch(rf"(then\s+)?(action|fork|join|merge|decide)\s+({NAME})(?:\s*:\s*({QNAME}))?", h)
        if m:
            nm = unq(m.group(3))
            node = ad.nodes.setdefault(nm, Node(nm))
            node.kind = m.group(2)
            node.type = last_seg(m.group(4)) if m.group(4) else None
            dur = duration_of(s.body)
            if dur is not None:
                node.duration = dur
            if m.group(1) and prev:
                ad.edges.append((prev, nm))
            prev = nm
            continue
        m = re.fullmatch(rf"then\s+({NAME})", h)
        if m and prev:
            tgt = unq(m.group(1))
            if tgt == "done":
                ad.nodes.setdefault("done", Node("done", "done"))
            else:
                ad.nodes.setdefault(tgt, Node(tgt))
            ad.edges.append((prev, tgt))
            continue
        model.unparsed.append((f"action def {name}", h))
    return ad


def parse_state_body(model, state, body):
    pending_entry = False
    for s in statements(body):
        h = s.header
        d = doc_ref(model, h)
        if d is not None:
            state.doc = d
            continue
        if h == "entry":
            pending_entry = True
            continue
        m = re.fullmatch(rf"then\s+({NAME})", h)
        if m and pending_entry:
            state.initial = unq(m.group(1))
            pending_entry = False
            continue
        m = re.fullmatch(rf"state\s+({NAME})(?:\s*:\s*({QNAME}))?", h)
        if m:
            child = State(unq(m.group(1)), parent=state)
            state.children[child.name] = child
            if s.body is not None:
                parse_state_body(model, child, s.body)
            continue
        m = re.fullmatch(
            rf"transition\s+({NAME})\s+first\s+({NAME})(?:\s+accept\s+(.+?))?(?:\s+if\s+(.+?))?\s+then\s+({NAME})", h)
        if m:
            trig = None
            if m.group(3):
                trig = last_seg(m.group(3).split(":")[-1])
            state.transitions.append(Transition(unq(m.group(1)), unq(m.group(2)), trig,
                                                m.group(4), unq(m.group(5))))
            continue
        m = re.fullmatch(rf"(?:do|entry|exit)\s+action\s+({NAME})(?:\s*:\s*({QNAME}))?", h)
        if m:
            state.actions.append(unq(m.group(1)))
            continue
        m = re.fullmatch(rf"do\s+({CHAIN})", h)
        if m:
            state.do_ref = m.group(1)
            continue
        model.unparsed.append((f"state {state.name}", h))


def parse_verification(model, vid, name, body):
    doc, sname, stype, verifies = "", None, None, []
    for s in statements(body or ""):
        d = doc_ref(model, s.header)
        if d is not None:
            doc = d
            continue
        m = re.fullmatch(rf"subject\s+({NAME})\s*:\s*({QNAME})", s.header)
        if m:
            sname, stype = unq(m.group(1)), last_seg(m.group(2))
            continue
        if s.header == "objective" and s.body is not None:
            for o in statements(s.body):
                mv = re.fullmatch(rf"verify\s+(?:requirement\s+)?({CHAIN})", o.header)
                if mv:
                    verifies.append(segs(mv.group(1))[-1])
                elif not o.header.startswith("doc "):
                    model.unparsed.append((f"verification {vid} objective", o.header))
            continue
        model.unparsed.append((f"verification {vid}", s.header))
    return Verification(vid, name, doc, sname, stype, verifies)


# --------------------------------------------------------------------------------------
# Name resolution
# --------------------------------------------------------------------------------------

def find_state(state, name):
    if state.name == name:
        return state
    for ch in state.children.values():
        hit = find_state(ch, name)
        if hit:
            return hit
    return None


def resolve_chain(model, chain):
    """Resolve a feature chain such as imagingInstrument.optics.focus.
    Returns (kind, type, obj) or raises ValueError with a readable reason."""
    parts = segs(chain)
    head = parts[0]
    if head not in model.usages:
        raise ValueError(f"'{head}' is not a top-level usage in the model")
    kind, typ = model.usages[head]
    obj = None
    if kind == "state" and typ in model.state_defs:
        obj = model.state_defs[typ].root
    path = head
    for seg in parts[1:]:
        if kind == "part":
            pd = model.part_defs.get(typ)
            if pd is None:
                raise ValueError(f"'{path}' has type '{typ}', which is not defined")
            if seg in pd.parts:
                kind, typ = "part", pd.parts[seg]
            elif seg in pd.ports:
                kind, typ = "port", pd.ports[seg][0]
            elif seg in pd.exhibits:
                kind, typ = "state", pd.exhibits[seg]
                sd = model.state_defs.get(typ)
                obj = sd.root if sd else None
            elif seg in pd.attrs:
                kind, typ = "attribute", pd.attrs[seg][0]
            else:
                raise ValueError(f"'{typ}' has no feature named '{seg}' (in {chain})")
        elif kind == "action":
            ad = model.action_defs.get(typ)
            node = ad.nodes.get(seg) if ad else None
            if node is None or node.kind != "action":
                raise ValueError(f"'{path}' has no action step named '{seg}'")
            kind, typ = "action", node.type
        elif kind == "state":
            if obj is not None and seg in obj.children:
                obj = obj.children[seg]
            elif obj is not None and seg in obj.actions:
                kind, typ, obj = "action", None, None
            else:
                raise ValueError(f"state '{path}' has no state or action named '{seg}'")
        else:
            raise ValueError(f"cannot navigate into '{path}' ({kind})")
        path += "." + seg
    return kind, typ, obj


# --------------------------------------------------------------------------------------
# Analysis evaluation
# --------------------------------------------------------------------------------------

MATH_FUNCS = {"sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "exp": math.exp,
              "ln": math.log, "log": math.log10, "sin": math.sin, "cos": math.cos,
              "tan": math.tan}
MATH_CONST = {"pi": math.pi}


class Evaluator:
    def __init__(self, model):
        self.model = model
        self.cache = {}
        self.design_inputs = {}   # (part def, attr) -> {chain: value}

    def attr(self, pd_name, attr, stack=()):
        key = (pd_name, attr)
        if key in self.cache:
            return self.cache[key]
        if key in stack:
            raise EvalError(f"circular reference at {pd_name}.{attr}")
        pd = self.model.part_defs.get(pd_name)
        if pd is None or attr not in pd.attrs:
            raise EvalError(f"unknown attribute {pd_name}.{attr}")
        expr = pd.attrs[attr][1]
        if expr is None:
            raise EvalError(f"{pd_name}.{attr} has no value")
        val = self.expr(expr, pd_name, {}, stack + (key,))
        self.cache[key] = val
        return val

    def expr(self, text, pd_name, local, stack):
        try:
            tree = ast.parse(text.replace("^", "**"), mode="eval")
        except SyntaxError:
            raise EvalError(f"cannot read expression '{text}'")
        return self._ev(tree.body, pd_name, local, stack)

    def _ev(self, n, pd_name, local, stack):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp):
            a, b = self._ev(n.left, pd_name, local, stack), self._ev(n.right, pd_name, local, stack)
            ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
                   ast.Div: lambda: a / b, ast.Pow: lambda: a ** b, ast.Mod: lambda: a % b}
            for op, fn in ops.items():
                if isinstance(n.op, op):
                    return fn()
            raise EvalError("unsupported operator")
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand, pd_name, local, stack)
            if isinstance(n.op, ast.USub):
                return -v
            if isinstance(n.op, ast.UAdd):
                return v
            raise EvalError("unsupported operator")
        if isinstance(n, ast.Name):
            if n.id in local:
                return local[n.id]
            if n.id in MATH_CONST:
                return MATH_CONST[n.id]
            if pd_name and n.id in self.model.part_defs[pd_name].attrs:
                return self.attr(pd_name, n.id, stack)
            raise EvalError(f"unknown name '{n.id}'")
        if isinstance(n, ast.Attribute):
            chain = dotted(n)
            parts = chain.split(".")
            try:
                kind, typ, _ = resolve_chain(self.model, ".".join(parts[:-1]))
            except ValueError as e:
                raise EvalError(str(e))
            if kind != "part" or typ not in self.model.part_defs:
                raise EvalError(f"'{chain}' does not point to a part attribute")
            val = self.attr(typ, parts[-1], stack)
            if stack:
                self.design_inputs.setdefault(stack[0], {})[chain] = val
            return val
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            args = [self._ev(a, pd_name, local, stack) for a in n.args]
            fname = n.func.id
            if fname in self.model.calcs:
                c = self.model.calcs[fname]
                if c.expr is None or len(args) != len(c.params):
                    raise EvalError(f"calc '{fname}' expects {len(c.params)} inputs")
                return self.expr(c.expr, None, dict(zip(c.params, args)), stack)
            if fname in MATH_FUNCS:
                return float(MATH_FUNCS[fname](*args))
            raise EvalError(f"unknown function '{fname}'")
        raise EvalError("unsupported expression")


def dotted(n):
    if isinstance(n, ast.Attribute):
        return dotted(n.value) + "." + n.attr
    if isinstance(n, ast.Name):
        return n.id
    raise EvalError("unsupported expression")


def is_literal(expr):
    return expr is not None and re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", expr.strip())


# --------------------------------------------------------------------------------------
# Value extraction for the verification consistency check
# --------------------------------------------------------------------------------------

UNIT_MAP = {"µm": "µm", "μm": "µm", "um": "µm", "mrad": "mrad", "c": "°C", "°c": "°C",
            "s": "s", "sec": "s", "second": "s", "seconds": "s", "min": "min", "minute": "min",
            "minutes": "min", "h": "h", "hour": "h", "hours": "h", "pixel": "px",
            "pixels": "px", "px": "px", "nm": "nm", "mm": "mm"}
VALUE_RE = re.compile(
    r"(?<![A-Za-z]-)(?<![\w.])(\d+(?:\.\d+)?)"
    r"(?:\s*(?:to|–|-|x|×)\s*(\d+(?:\.\d+)?))?"
    r"\s*(µm|μm|um|mrad|°\s?C|C(?![A-Za-z])|seconds?|sec(?![A-Za-z])|s(?![A-Za-z])|minutes?|"
    r"min(?![A-Za-z])|hours?|h(?![A-Za-z])|pixels?|px|nm|mm)?")


def values_in(text):
    vals = set()
    for m in VALUE_RE.finditer(text or ""):
        unit = m.group(3)
        unit = UNIT_MAP.get(unit.replace(" ", "").lower(), unit) if unit else None
        for g in (m.group(1), m.group(2)):
            if g is not None:
                vals.add((float(g), unit))
    return vals


def show_value(v):
    num, unit = v
    return f"{fmt_num(num)} {unit}" if unit else fmt_num(num)


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------

class Checker:
    def __init__(self, model):
        self.m = model
        self.findings = []
        self.sections = []

    def add(self, level, check, message):
        self.findings.append(Finding(level, check, message))

    # ---- shared lookups -------------------------------------------------------------
    def parents(self):
        p = {}
        for orig, der in self.m.derivations:
            p.setdefault(der, []).append(orig)
        return p

    def children(self):
        c = {}
        for orig, der in self.m.derivations:
            c.setdefault(orig, []).append(der)
        return c

    def satisfied_by(self):
        s = {}
        for req, chain in self.m.satisfies:
            s.setdefault(req, []).append(chain)
        return s

    def verified_by(self):
        v = {}
        for ver in self.m.verifications.values():
            for r in ver.verifies:
                v.setdefault(r, []).append(ver.vid)
        return v

    def reqs_sorted(self):
        return sorted(self.m.requirements.values(),
                      key=lambda r: (not r.rid.startswith(TOP_LEVEL_PREFIXES), natural_key(r.rid)))

    def rid(self, name):
        r = self.m.requirements.get(name)
        return r.rid if r else name

    # ---- 1. references and coverage -------------------------------------------------
    def check_references(self):
        m = self.m
        for req, chain in m.satisfies:
            if req not in m.requirements:
                self.add("ERROR", "References", f"satisfy refers to unknown requirement '{req}'")
            try:
                resolve_chain(m, chain)
            except ValueError as e:
                self.add("ERROR", "References", f"satisfy {req} by {chain}: {e}")
        for orig, der in m.derivations:
            for r in (orig, der):
                if r not in m.requirements:
                    self.add("ERROR", "References", f"derivation refers to unknown requirement '{r}'")
        for v in m.verifications.values():
            for r in v.verifies:
                if r not in m.requirements:
                    self.add("ERROR", "References", f"{v.vid} verifies unknown requirement '{r}'")
            if v.subject_type and v.subject_type not in m.part_defs:
                self.add("ERROR", "References", f"{v.vid} subject type '{v.subject_type}' is not defined")
        for f, p in m.allocations:
            for chain in (f, p):
                try:
                    resolve_chain(m, chain)
                except ValueError as e:
                    self.add("ERROR", "References", f"allocate {f} to {p}: {e}")
        for pd in m.part_defs.values():
            for pname, ptype in pd.parts.items():
                if ptype not in m.part_defs:
                    self.add("ERROR", "References", f"{pd.name}.{pname} has undefined type '{ptype}'")
            for port, (ptype, _) in pd.ports.items():
                if ptype not in m.port_defs:
                    self.add("ERROR", "References", f"{pd.name}.{port} has undefined port type '{ptype}'")

    def section_coverage(self):
        parents, sat, ver = self.parents(), self.satisfied_by(), self.verified_by()
        rows, gaps = [], 0
        for r in self.reqs_sorted():
            missing = []
            top = r.rid.startswith(TOP_LEVEL_PREFIXES)
            p = parents.get(r.name, [])
            if not p and not top:
                missing.append("parent")
            if not sat.get(r.name):
                missing.append("satisfy")
            if not ver.get(r.name):
                missing.append("verify")
            status = "OK" if not missing else "MISSING " + ", ".join(missing)
            if missing:
                gaps += 1
                self.add("ERROR", "Coverage", f"{r.rid} {r.name}: no {' / '.join(missing)}")
            rows.append([r.rid, r.name,
                         "(top level)" if top and not p else ", ".join(self.rid(x) for x in p) or "—",
                         ", ".join(short_chain(c) for c in sat.get(r.name, [])) or "—",
                         ", ".join(ver.get(r.name, [])) or "—",
                         status])
        self.coverage_rows = rows
        blocks = [("p", f"Rule: every requirement needs a satisfy (what meets it) and a verify "
                        f"(how it is proven). Every requirement except top-level ones "
                        f"(ID starting {', '.join(TOP_LEVEL_PREFIXES)}) also needs a parent. "
                        f"Gaps found: **{gaps}**."),
                  ("table", ["ID", "Requirement", "Derived from", "Satisfied by",
                             "Verified by", "Status"], rows)]
        self.sections.append(("1. Requirement coverage", blocks))

    # ---- 2. requirement quality --------------------------------------------------------
    def section_quality(self):
        rows = []
        for r in self.reqs_sorted():
            issues = []
            text = r.doc.lower()
            if not r.doc:
                issues.append("no requirement text")
            else:
                n_shall = len(re.findall(r"\bshall\b", text))
                if n_shall == 0:
                    issues.append('no "shall"')
                elif n_shall > 1:
                    issues.append(f'{n_shall} "shall" statements (split into separate requirements?)')
                if not values_in(r.doc):
                    issues.append("no measurable value")
                for w in VAGUE_WORDS:
                    if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text):
                        issues.append(f'vague word "{w}"')
            for i in issues:
                self.add("WARNING", "Requirement quality", f"{r.rid} {r.name}: {i}")
            rows.append([r.rid, r.doc or "—", "OK" if not issues else "; ".join(issues)])
        self.sections.append(("2. Requirement quality", [
            ("p", 'Rule-based lint: one "shall" per requirement, a measurable value, and none of '
                  'the usual vague words (fast, sharp, user-friendly, approximately, ...).'),
            ("table", ["ID", "Requirement text", "Result"], rows)]))

    # ---- 3. verification consistency ----------------------------------------------------
    def section_verification(self):
        rows = []
        order = list(METHOD_PREFIX.values())

        def vkey(v):
            p = re.match(r"[A-Za-z]+", v.vid).group(0).upper()
            return (order.index(p) if p in order else len(order), natural_key(v.vid))

        for v in sorted(self.m.verifications.values(), key=vkey):
            notes = []
            mm = re.search(r"Method:\s*([A-Za-z]+)", v.doc)
            method = mm.group(1) if mm else None
            prefix = re.match(r"[A-Za-z]+", v.vid).group(0).upper()
            if method is None:
                notes.append('no "Method:" stated')
                self.add("WARNING", "Verification", f'{v.vid}: no "Method:" stated in the description')
            elif method.lower() not in METHOD_PREFIX:
                notes.append(f'method "{method}" is not Test, Analysis, Inspection or Demonstration')
                self.add("WARNING", "Verification",
                         f'{v.vid}: method "{method}" is not Test, Analysis, Inspection or Demonstration')
            elif METHOD_PREFIX[method.lower()] != prefix:
                notes.append(f"method {method} does not match ID prefix {prefix}")
                self.add("WARNING", "Verification", f"{v.vid}: method {method} does not match ID prefix {prefix}")
            if "pass" not in v.doc.lower():
                notes.append("no pass criterion")
                self.add("WARNING", "Verification", f"{v.vid}: no pass criterion stated")
            have = values_in(v.doc)
            for rname in v.verifies:
                r = self.m.requirements.get(rname)
                if not r:
                    continue
                missing = sorted(values_in(r.doc) - have)
                if missing:
                    shown = ", ".join(show_value(x) for x in missing)
                    notes.append(f"does not state {shown} from {r.rid}")
                    self.add("WARNING", "Verification",
                             f"{v.vid} verifies {r.rid} {r.name} but does not state {shown}")
            rows.append([v.vid, v.name, method or "—",
                         ", ".join(self.rid(x) for x in v.verifies),
                         "OK" if not notes else "; ".join(notes)])
        self.sections.append(("3. Verification consistency", [
            ("p", "Each verification activity should name a standard method (Test, Analysis, "
                  "Inspection, Demonstration) that matches its ID prefix, state a pass criterion, "
                  "and mention every value (with units) from the requirements it verifies. "
                  "The value check is a text heuristic: it flags likely mismatches for a person "
                  "to review."),
            ("table", ["ID", "Activity", "Method", "Verifies", "Result"], rows)]))

    # ---- 4. functional allocation --------------------------------------------------------
    def functions(self):
        """Leaf functions: steps of top-level action usages (expanding steps typed by an
        action def into that def's steps) and actions declared inside exhibited states."""
        out = []

        def walk_action(prefix, adef_name, seen):
            ad = self.m.action_defs.get(adef_name)
            if not ad or adef_name in seen:
                return
            for nm, node in ad.nodes.items():
                if node.kind != "action":
                    continue
                path = f"{prefix}.{nm}"
                if node.type in self.m.action_defs:
                    walk_action(path, node.type, seen | {adef_name})
                else:
                    out.append(path)

        for uname, (kind, typ) in self.m.usages.items():
            if kind == "action":
                walk_action(uname, typ, set())
        for uname, (kind, typ) in self.m.usages.items():
            if kind != "part" or typ not in self.m.part_defs:
                continue
            for ename, sdef in self.m.part_defs[typ].exhibits.items():
                sd = self.m.state_defs.get(sdef)
                if not sd:
                    continue

                def walk_state(st, path):
                    for a in st.actions:
                        out.append(f"{path}.{a}")
                    for ch in st.children.values():
                        walk_state(ch, f"{path}.{ch.name}")
                walk_state(sd.root, f"{uname}.{ename}")
        return out

    def part_paths(self):
        out = []

        def walk(prefix, ptype, depth):
            pd = self.m.part_defs.get(ptype)
            if not pd or depth > 10:
                return
            for nm, t in pd.parts.items():
                out.append(f"{prefix}.{nm}")
                walk(f"{prefix}.{nm}", t, depth + 1)

        for uname, (kind, typ) in self.m.usages.items():
            if kind == "part":
                out.append(uname)
                walk(uname, typ, 0)
        return out

    def alloc_map(self):
        amap = {}
        for f, p in self.m.allocations:
            amap.setdefault(norm_chain(f), []).append(norm_chain(p))
        return amap

    def section_allocation(self):
        funcs = self.functions()
        amap = self.alloc_map()
        rows, unalloc = [], []
        for f in funcs:
            parts = []
            for af, ps in amap.items():
                if f == af or f.startswith(af + "."):
                    parts.extend(ps)
            if not parts:
                unalloc.append(f)
                self.add("WARNING", "Allocation", f"function {f} is not allocated to any part")
            rows.append([f, ", ".join(short_chain(p) for p in parts) or "— (not allocated)"])
        part_rows = []
        sat_chains = [norm_chain(c) for _, c in self.m.satisfies]
        alloc_parts = [p for ps in amap.values() for p in ps]
        for p in self.part_paths():
            funcs_here = sorted({f for f, ps in amap.items() for q in ps if q == p})
            reqs_here = sorted({self.rid(r) for r, c in self.m.satisfies if norm_chain(c) == p})
            busy_below = any(q.startswith(p + ".") for q in alloc_parts + sat_chains)
            if not funcs_here and not reqs_here and not busy_below:
                self.add("WARNING", "Allocation",
                         f"part {p} has no allocated function and satisfies no requirement")
                status = "no function, no requirement"
            else:
                status = "OK"
            part_rows.append([short_chain(p) or p,
                              ", ".join(short_chain(f) for f in funcs_here) or "—",
                              ", ".join(reqs_here) or "—", status])
        self.sections.append(("4. Functional allocation", [
            ("p", "Every function (workflow step or state activity) should be allocated to a part, "
                  "and every part should either perform a function or satisfy a requirement."),
            ("table", ["Function", "Allocated to"], rows),
            ("table", ["Part", "Functions", "Satisfies", "Status"], part_rows)]))

    # ---- 5. workflows ---------------------------------------------------------------------
    def section_workflows(self):
        blocks = []
        amap = self.alloc_map()
        for ad in self.m.action_defs.values():
            blocks.append(("h3", f"{ad.name}"))
            if ad.doc:
                blocks.append(("p", ad.doc))
            declared = {n for n, nd in ad.nodes.items() if nd.kind}
            for a, b in ad.edges:
                for x in (a, b):
                    if x not in declared:
                        self.add("ERROR", "Workflow", f"{ad.name}: step '{x}' is used but never declared")
            out_deg = {n: 0 for n in ad.nodes}
            in_deg = {n: 0 for n in ad.nodes}
            for a, b in ad.edges:
                out_deg[a] = out_deg.get(a, 0) + 1
                in_deg[b] = in_deg.get(b, 0) + 1
            dead = [n for n, nd in ad.nodes.items() if nd.kind not in ("done", None) and out_deg.get(n, 0) == 0]
            for n in dead:
                self.add("ERROR", "Workflow",
                         f"{ad.name}: '{n}' has no outgoing flow, so the workflow cannot continue "
                         f"after it (missing 'then')")
            reach = reachable(ad)
            for n, nd in ad.nodes.items():
                if nd.kind and nd.kind != "start" and n not in reach:
                    self.add("ERROR", "Workflow", f"{ad.name}: '{n}' can never be reached from start")
            for n, nd in ad.nodes.items():
                if nd.kind == "join" and in_deg.get(n, 0) < 2:
                    self.add("WARNING", "Workflow",
                             f"{ad.name}: join '{n}' has {in_deg.get(n, 0)} incoming flow(s); "
                             f"a join normally waits for every branch of its fork")
                if nd.kind == "fork" and out_deg.get(n, 0) < 2:
                    self.add("WARNING", "Workflow", f"{ad.name}: fork '{n}' has fewer than 2 branches")
            crit = critical_path(ad)
            crit_nodes, crit_edges, slack = set(), set(), {}
            if crit and any(nd.duration is not None for nd in ad.nodes.values()):
                total, ef, slack = crit
                crit_nodes = {n for n, sl in slack.items() if abs(sl) < 1e-9}
                durs = {n: (nd.duration or 0.0) for n, nd in ad.nodes.items()}
                crit_edges = {(a, b) for a, b in ad.edges if a in crit_nodes and b in crit_nodes
                              and abs(ef[a] + durs[b] - ef[b]) < 1e-9}
                steps = [n for n in ad.nodes if n in crit_nodes and ad.nodes[n].kind == "action"]
                limit = re.search(r"(SR-\d+)?[^.]*?allows\s+(\d+(?:\.\d+)?)\s*minutes", ad.doc or "")
                text = (f"Critical path: **{fmt_num(total)} min**. Steps with zero slack: "
                        f"{', '.join(steps)}.")
                if limit:
                    lim = float(limit.group(2))
                    margin = lim - total
                    text += (f" Limit {fmt_num(lim)} min"
                             f"{' (' + limit.group(1) + ')' if limit.group(1) else ''}: "
                             f"margin **{fmt_num(margin)} min**.")
                    if margin < 0:
                        self.add("ERROR", "Workflow", f"{ad.name}: critical path {fmt_num(total)} min "
                                                      f"exceeds the {fmt_num(lim)} min limit")
                blocks.append(("p", text))
            timed = any(nd.duration is not None for nd in ad.nodes.values())
            typed = any(nd.type for nd in ad.nodes.values() if nd.kind == "action")
            rows = []
            for n, nd in ad.nodes.items():
                if nd.kind != "action":
                    continue
                row = [n] + ([nd.type or ""] if typed else [])
                if timed:
                    row += [fmt_num(nd.duration) if nd.duration is not None else "",
                            fmt_num(slack[n]) if n in slack else "not on a path to done",
                            "yes" if n in crit_nodes else ""]
                rows.append(row)
            if rows:
                heads = (["Step"] + (["Type"] if typed else [])
                         + (["Duration (min)", "Slack (min)", "Critical"] if timed else []))
                blocks.append(("table", heads, rows))
            blocks.append(("mermaid", workflow_mermaid(self.m, ad, amap, crit_nodes, crit_edges, set(dead))))
        self.sections.append(("5. Workflows", blocks))

    # ---- 6. state machines -----------------------------------------------------------------
    def section_states(self):
        blocks = []
        signals = self.m.attr_defs | self.m.item_defs
        for sd in self.m.state_defs.values():
            blocks.append(("h3", sd.name))
            if sd.doc:
                blocks.append(("p", sd.doc))
            all_states = list(iter_states(sd.root))
            leaves = [s for s in all_states if not s.children and s is not sd.root]
            by_name = {s.name: s for s in all_states}
            trans = [t for s in all_states for t in s.transitions]
            for t in trans:
                for x in (t.src, t.tgt):
                    if x not in by_name:
                        self.add("ERROR", "State machine", f"{sd.name}: transition {t.name} refers to unknown state '{x}'")
                if t.trigger and t.trigger not in signals:
                    self.add("ERROR", "State machine", f"{sd.name}: trigger '{t.trigger}' in {t.name} is not defined")
            for s in all_states:
                if s.children and not s.initial:
                    self.add("WARNING", "State machine", f"{sd.name}: composite state '{s.name}' has no initial state")
                if s.do_ref:
                    try:
                        resolve_chain(self.m, s.do_ref)
                    except ValueError as e:
                        self.add("ERROR", "State machine", f"{sd.name}: state '{s.name}' do {s.do_ref}: {e}")

            def enter(name):
                st = by_name.get(name)
                while st is not None and st.children and st.initial:
                    st = by_name.get(st.initial)
                return st

            def outgoing(leaf):
                names, st = set(), leaf
                while st is not None:
                    names.add(st.name)
                    st = st.parent
                return [t for t in trans if t.src in names]

            start = enter(sd.root.initial) if sd.root.initial else None
            seen, queue = set(), [start] if start else []
            while queue:
                st = queue.pop()
                if st is None or st.name in seen:
                    continue
                seen.add(st.name)
                for t in outgoing(st):
                    queue.append(enter(t.tgt))
            for s in leaves:
                if s.name not in seen:
                    self.add("ERROR", "State machine", f"{sd.name}: state '{s.name}' can never be reached")
                if not outgoing(s):
                    self.add("WARNING", "State machine", f"{sd.name}: state '{s.name}' has no way out")
            for t in trans:
                src = by_name.get(t.src)
                if t.trigger is None and src is not None and not (src.do_ref or src.actions):
                    self.add("WARNING", "State machine",
                             f"{sd.name}: {t.name} has no trigger and '{t.src}' has no activity, "
                             f"so it would fire immediately")
            rows = []
            for i, t in enumerate(trans, 1):
                src = by_name.get(t.src)
                frm = t.src + (" (any state inside)" if src is not None and src.children else "")
                tgt_leaf = enter(t.tgt)
                expect = t.tgt + (f" → {tgt_leaf.name}" if tgt_leaf is not None and tgt_leaf.name != t.tgt else "")
                event = t.trigger or "activity completes"
                if t.guard:
                    event += f" [if {t.guard}]"
                rows.append([f"TC-{i:02d}", frm, event, expect])
            blocks.append(("p", f"{len(leaves)} states, {len(trans)} transitions. Each transition "
                                f"below is one test case: put the instrument in the starting "
                                f"state, cause the event, and confirm the expected state."))
            blocks.append(("table", ["Test case", "From state", "Event", "Expected state"], rows))
            blocks.append(("mermaid", state_mermaid(sd)))
        self.sections.append(("6. State machines", blocks))

    # ---- 7. analysis --------------------------------------------------------------------
    def section_analysis(self):
        ev = Evaluator(self.m)
        blocks = []
        for pd in self.m.part_defs.values():
            if not any(e is not None and not is_literal(e) for _, e in pd.attrs.values()):
                continue
            blocks.append(("h3", pd.name))
            if pd.doc:
                blocks.append(("p", pd.doc))
            rows = []
            for an, (_, expr) in pd.attrs.items():
                try:
                    val = ev.attr(pd.name, an)
                    shown = fmt_num(val)
                except EvalError as e:
                    val, shown = None, f"could not evaluate: {e}"
                    self.add("WARNING", "Analysis", f"{pd.name}.{an}: {e}")
                rows.append([an, "" if is_literal(expr) else (expr or ""), shown])
                if an.lower().startswith("margin") and val is not None and val < 0:
                    self.add("ERROR", "Analysis", f"{pd.name}.{an} is negative ({fmt_num(val)})")
            blocks.append(("table", ["Attribute", "Formula", "Value"], rows))
            inputs = {}
            for (pdn, _), chains in ev.design_inputs.items():
                if pdn == pd.name:
                    inputs.update(chains)
            if inputs:
                blocks.append(("list", [f"from the design: `{c}` = {fmt_num(v)}" for c, v in inputs.items()]))
        if not blocks:
            blocks.append(("p", "No computed attributes found."))
        self.sections.append(("7. Analysis (computed from the model)", blocks))

    # ---- 8. diagrams ---------------------------------------------------------------------
    def section_diagrams(self):
        blocks = []
        for uname, (kind, typ) in self.m.usages.items():
            if kind == "part" and typ in self.m.part_defs and self.m.part_defs[typ].parts:
                blocks.append(("h3", f"Structure and interfaces: {uname}"))
                blocks.append(("mermaid", structure_mermaid(self.m, uname, typ)))
        blocks.append(("h3", "Requirement tree"))
        blocks.append(("mermaid", req_tree_mermaid(self)))
        for r in self.reqs_sorted():
            if r.rid.startswith(TOP_LEVEL_PREFIXES):
                blocks.append(("h3", f"Traceability thread: {r.rid} {r.name}"))
                blocks.append(("p", "Solid arrows: requirement derived from its parent. Green: the "
                                    "part that satisfies it. Orange: the activity that verifies it."))
                blocks.append(("mermaid", thread_mermaid(self, r.name)))
        self.sections.append(("8. Diagrams generated from the model", blocks))

    def section_notes(self):
        items = [f"{ctx}: `{h}`" for ctx, h in self.m.unparsed]
        blocks = [("p", "Statements this checker does not interpret (shown so nothing is "
                        "skipped silently):" if items else
                        "Every statement in the model was interpreted.")]
        if items:
            blocks.append(("list", items))
        self.sections.append(("Notes", blocks))

    def run(self):
        self.check_references()
        self.section_coverage()
        self.section_quality()
        self.section_verification()
        self.section_allocation()
        self.section_workflows()
        self.section_states()
        self.section_analysis()
        self.section_diagrams()
        self.section_notes()
        return self


def norm_chain(c):
    return ".".join(segs(c))


def short_chain(c):
    """imagingInstrument.optics.focus -> optics.focus (top-level instrument name dropped)."""
    s = segs(c)
    return ".".join(s[1:]) if len(s) > 1 else ".".join(s)


def iter_states(st):
    yield st
    for ch in st.children.values():
        yield from iter_states(ch)


def reachable(ad):
    adj = {}
    for a, b in ad.edges:
        adj.setdefault(a, []).append(b)
    seen, stack = set(), ["start"] if "start" in ad.nodes else []
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, []))
    return seen


def critical_path(ad):
    """Critical-path analysis over step durations (start -> done).
    Returns (total, earliest_finish, slack) or None if not computable. Steps with
    zero slack are on the critical path; parallel branches can tie."""
    if "start" not in ad.nodes or "done" not in ad.nodes:
        return None
    succ, pred, indeg = {n: [] for n in ad.nodes}, {n: [] for n in ad.nodes}, {n: 0 for n in ad.nodes}
    for a, b in ad.edges:
        succ.setdefault(a, []).append(b)
        pred.setdefault(b, []).append(a)
        indeg[b] = indeg.get(b, 0) + 1
    order, queue = [], [n for n in ad.nodes if indeg.get(n, 0) == 0]
    while queue:
        n = queue.pop()
        order.append(n)
        for b in succ.get(n, []):
            indeg[b] -= 1
            if indeg[b] == 0:
                queue.append(b)
    if len(order) != len(ad.nodes):
        return None  # cycle
    dur = {n: (nd.duration or 0.0) for n, nd in ad.nodes.items()}
    ef = {"start": 0.0}
    for n in order:
        if n == "start" or not any(p in ef for p in pred.get(n, [])):
            continue
        ef[n] = max(ef[p] for p in pred[n] if p in ef) + dur[n]
    if "done" not in ef:
        return None
    lf = {"done": ef["done"]}
    for n in reversed(order):
        if n == "done" or n not in ef:
            continue
        nxt = [lf[b] - dur[b] for b in succ.get(n, []) if b in lf]
        if nxt:
            lf[n] = min(nxt)
    slack = {n: lf[n] - ef[n] for n in lf if n in ef}
    return ef["done"], ef, slack


# --------------------------------------------------------------------------------------
# Mermaid diagrams
# --------------------------------------------------------------------------------------

def mid(prefix, name):
    return prefix + re.sub(r"\W", "_", name)


def mlabel(s):
    return s.replace('"', "#quot;")


def workflow_mermaid(model, ad, amap, crit_nodes, crit_edges, dead):
    lines = ["flowchart LR"]
    prefixes = [u for u, (k, t) in model.usages.items() if k == "action" and t == ad.name]
    owner = prefixes[0] if prefixes else None

    def parts_for(path):
        return [short_chain(p) for p in amap.get(path, [])]

    for nm, nd in ad.nodes.items():
        i = mid("a_", nm)
        if nd.kind == "start":
            lines.append(f'  {i}(("start"))')
        elif nd.kind == "done":
            lines.append(f'  {i}((("done")))')
        elif nd.kind in ("fork", "join", "merge", "decide"):
            lines.append(f'  {i}["{nd.kind}"]:::bar')
        else:
            label = f"<b>{nm}</b>" + (f" : {nd.type}" if nd.type else "")
            info = []
            if nd.duration is not None:
                info.append(f"{fmt_num(nd.duration)} min")
            if owner:
                ps = parts_for(f"{owner}.{nm}")
                if ps:
                    info.append("→ " + ", ".join(ps))
            if info:
                label += "<br/>" + " · ".join(info)
            sub = model.action_defs.get(nd.type) if nd.type else None
            if sub:
                steps = []
                for sn, snd in sub.nodes.items():
                    if snd.kind != "action":
                        continue
                    ps = parts_for(f"{owner}.{nm}.{sn}") if owner else []
                    steps.append(sn + (f" ({', '.join(ps)})" if ps else ""))
                label += "<br/><i>" + " → ".join(steps) + "</i>"
            cls = ":::dead" if nm in dead else (":::crit" if nm in crit_nodes else "")
            lines.append(f'  {i}["{mlabel(label)}"]{cls}')
    crit_idx = []
    for idx, (a, b) in enumerate(ad.edges):
        lines.append(f"  {mid('a_', a)} --> {mid('a_', b)}")
        if (a, b) in crit_edges:
            crit_idx.append(str(idx))
    lines.append("  classDef bar fill:#333,color:#fff,stroke:#333")
    lines.append("  classDef crit stroke:#1f6feb,stroke-width:3px")
    lines.append("  classDef dead fill:#fdecea,stroke:#c0392b,stroke-width:3px,stroke-dasharray:6 3")
    if crit_idx:
        lines.append(f"  linkStyle {','.join(crit_idx)} stroke:#1f6feb,stroke-width:3px")
    return "\n".join(lines)


def state_mermaid(sd):
    lines = ["stateDiagram-v2"]

    def sid(n):
        return mid("s_", n)

    def emit(st, depth):
        pad = "  " * depth
        for ch in st.children.values():
            lines.append(f'{pad}state "{ch.name}" as {sid(ch.name)}')
        if st.initial:
            lines.append(f"{pad}[*] --> {sid(st.initial)}")
        for ch in st.children.values():
            if ch.children:
                lines.append(f"{pad}state {sid(ch.name)} {{")
                emit(ch, depth + 1)
                lines.append(f"{pad}}}")
            acts = ([f"do {ch.do_ref}"] if ch.do_ref else []) + [f"do {a}" for a in ch.actions]
            for a in acts:
                lines.append(f"{pad}{sid(ch.name)} : {a}")
        for t in st.transitions:
            lbl = t.trigger or "activity done"
            lines.append(f"{pad}{sid(t.src)} --> {sid(t.tgt)} : {lbl}")

    emit(sd.root, 1)
    return "\n".join(lines)


def connection_flows(model, pd, end_a, end_b):
    def end_info(end):
        s = segs(end)
        if len(s) != 2 or s[0] not in pd.parts:
            return None
        child = model.part_defs.get(pd.parts[s[0]])
        if not child or s[1] not in child.ports:
            return None
        ptype, conj = child.ports[s[1]]
        return s[0], ptype, conj

    a, b = end_info(end_a), end_info(end_b)
    if not a or not b:
        return []
    flows = []
    for direction, _, itype in model.port_defs.get(a[1], []):
        eff = direction
        if a[2]:
            eff = {"in": "out", "out": "in"}.get(direction, direction)
        if eff in ("out", "inout"):
            flows.append((a[0], b[0], itype))
        if eff in ("in", "inout"):
            flows.append((b[0], a[0], itype))
    return flows


def structure_mermaid(model, uname, typ):
    pd = model.part_defs[typ]
    lines = ["flowchart LR", f'  subgraph {mid("u_", uname)}["{uname} : {typ}"]']
    for pname, ptype in pd.parts.items():
        child = model.part_defs.get(ptype)
        label = f"<b>{pname}</b> : {ptype}"
        if child and child.parts:
            label += "<br/><i>" + " · ".join(child.parts) + "</i>"
        lines.append(f'    {mid("p_", pname)}["{mlabel(label)}"]')
    lines.append("  end")
    grouped = {}
    for a, b in pd.connections:
        for src, dst, item in connection_flows(model, pd, a, b):
            grouped.setdefault((src, dst), []).append(item)
    for (src, dst), items in grouped.items():
        lines.append(f'  {mid("p_", src)} -- "{", ".join(items)}" --> {mid("p_", dst)}')
    return "\n".join(lines)


def req_node(ch, name):
    r = ch.m.requirements.get(name)
    rid = r.rid if r else name
    return f'  {mid("r_", name)}("<b>{rid}</b><br/>{name}")' + (":::top" if rid.startswith(TOP_LEVEL_PREFIXES) else "")


def req_tree_mermaid(ch):
    lines = ["flowchart TB"]
    for r in ch.reqs_sorted():
        lines.append(req_node(ch, r.name))
    for orig, der in ch.m.derivations:
        lines.append(f'  {mid("r_", orig)} -->|derive| {mid("r_", der)}')
    lines.append("  classDef top fill:#e8f0fe,stroke:#1f6feb,stroke-width:2px")
    return "\n".join(lines)


def thread_mermaid(ch, top):
    kids = ch.children()
    reqs, stack = [], [top]
    while stack:
        n = stack.pop(0)
        if n in reqs:
            continue
        reqs.append(n)
        stack.extend(kids.get(n, []))
    sat, ver = ch.satisfied_by(), ch.verified_by()
    lines = ["flowchart LR"]
    for r in reqs:
        lines.append(req_node(ch, r))
    for orig, der in ch.m.derivations:
        if orig in reqs and der in reqs:
            lines.append(f'  {mid("r_", orig)} --> {mid("r_", der)}')
    done_parts, done_tests = set(), set()
    for r in reqs:
        for c in sat.get(r, []):
            pid = mid("p_", norm_chain(c))
            if pid not in done_parts:
                lines.append(f'  {pid}["{mlabel(short_chain(c) or c)}"]:::part')
                done_parts.add(pid)
            lines.append(f"  {pid} -.->|satisfies| {mid('r_', r)}")
        for vid in ver.get(r, []):
            tid = mid("t_", vid)
            if tid not in done_tests:
                v = ch.m.verifications[vid]
                lines.append(f'  {tid}[/"{mlabel(vid + " " + v.name)}"/]:::test')
                done_tests.add(tid)
            lines.append(f"  {mid('r_', r)} -.->|verified by| {tid}")
    lines.append("  classDef top fill:#e8f0fe,stroke:#1f6feb,stroke-width:2px")
    lines.append("  classDef part fill:#eef7ee,stroke:#2e7d32")
    lines.append("  classDef test fill:#fff4e5,stroke:#b26a00")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Report writers
# --------------------------------------------------------------------------------------

def md_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def write_markdown(ch, path, source_name):
    errs = [f for f in ch.findings if f.level == "ERROR"]
    warns = [f for f in ch.findings if f.level == "WARNING"]
    out = [f"# Model check report: {ch.m.root_name}", "",
           f"Source: `{source_name}` · generated by `tools/model_check.py`", "",
           "## Summary", "",
           f"- {len(ch.m.requirements)} requirements, {len(ch.m.verifications)} verification "
           f"activities, {len(ch.functions())} functions, {len(ch.part_paths())} parts",
           f"- **{plural(len(errs), 'error')}, {plural(len(warns), 'warning')}**", ""]
    for f in errs + warns:
        out.append(f"- **{f.level}** · {f.check}: {f.message}")
    out.append("")
    for title, blocks in ch.sections:
        out += [f"## {title}", ""]
        for b in blocks:
            kind = b[0]
            if kind == "p":
                out += [b[1], ""]
            elif kind == "h3":
                out += [f"### {b[1]}", ""]
            elif kind == "list":
                out += [f"- {x}" for x in b[1]] + [""]
            elif kind == "table":
                out.append("| " + " | ".join(b[1]) + " |")
                out.append("|" + "---|" * len(b[1]))
                out += ["| " + " | ".join(md_cell(c) for c in row) + " |" for row in b[2]]
                out.append("")
            elif kind == "mermaid":
                out += ["```mermaid", b[1], "```", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def inline_md(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def write_html(ch, path, source_name):
    errs = [f for f in ch.findings if f.level == "ERROR"]
    warns = [f for f in ch.findings if f.level == "WARNING"]
    css = """
:root{--bg:#fff;--fg:#1d2330;--mut:#5b6474;--line:#dfe3ea;--err:#b3261e;--errbg:#fdecea;
--warn:#8a5a00;--warnbg:#fff4e5;--ok:#1e7a35;--okbg:#e8f5ec;--head:#f5f7fa}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px} h2{font-size:20px;margin:36px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:16px;margin:22px 0 8px} p,li{max-width:80ch} .mut{color:var(--mut)}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:14px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--head)} code{font:13px ui-monospace,Consolas,monospace;background:var(--head);padding:1px 4px;border-radius:4px}
.f{padding:6px 10px;border-radius:6px;margin:4px 0;font-size:14px}
.ERROR{background:var(--errbg);color:var(--err)} .WARNING{background:var(--warnbg);color:var(--warn)}
td.OK{color:var(--ok);background:var(--okbg)} td.BAD{color:var(--err);background:var(--errbg)}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;margin-right:6px;font-weight:600}
pre.mermaid{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
"""
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width,initial-scale=1'>",
           f"<title>Model check: {html.escape(ch.m.root_name)}</title><style>{css}</style></head><body><main>",
           f"<h1>Model check report: {html.escape(ch.m.root_name)}</h1>",
           f"<p class='mut'>Source: <code>{html.escape(source_name)}</code> · generated by "
           f"<code>tools/model_check.py</code></p>",
           f"<p><span class='pill ERROR'>{plural(len(errs), 'error')}</span>"
           f"<span class='pill WARNING'>{plural(len(warns), 'warning')}</span> "
           f"{len(ch.m.requirements)} requirements · {len(ch.m.verifications)} verification activities · "
           f"{len(ch.functions())} functions · {len(ch.part_paths())} parts</p>"]
    for f in errs + warns:
        out.append(f"<div class='f {f.level}'><strong>{f.level}</strong> · {html.escape(f.check)}: "
                   f"{html.escape(f.message)}</div>")
    for title, blocks in ch.sections:
        out.append(f"<h2>{html.escape(title)}</h2>")
        for b in blocks:
            kind = b[0]
            if kind == "p":
                out.append(f"<p>{inline_md(b[1])}</p>")
            elif kind == "h3":
                out.append(f"<h3>{html.escape(b[1])}</h3>")
            elif kind == "list":
                out.append("<ul>" + "".join(f"<li>{inline_md(x)}</li>" for x in b[1]) + "</ul>")
            elif kind == "table":
                out.append("<table><thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in b[1])
                           + "</tr></thead><tbody>")
                for row in b[2]:
                    cells = []
                    for j, c in enumerate(row):
                        cls = ""
                        if b[1][j] in ("Status", "Result"):
                            cls = " class='OK'" if c == "OK" else " class='BAD'"
                        cells.append(f"<td{cls}>{html.escape(str(c))}</td>")
                    out.append("<tr>" + "".join(cells) + "</tr>")
                out.append("</tbody></table>")
            elif kind == "mermaid":
                out.append(f"<pre class='mermaid'>{html.escape(b[1])}</pre>")
    out.append(f"<script src='{MERMAID_CDN}'></script>")
    out.append("<script>mermaid.initialize({startOnLoad:true,securityLevel:'loose',"
               "flowchart:{htmlLabels:true,useMaxWidth:false},"
               "state:{useMaxWidth:false}});</script></main></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def write_outputs(ch, out_dir, source_name):
    os.makedirs(os.path.join(out_dir, "diagrams"), exist_ok=True)
    write_markdown(ch, os.path.join(out_dir, "model_report.md"), source_name)
    write_html(ch, os.path.join(out_dir, "model_report.html"), source_name)
    with open(os.path.join(out_dir, "coverage_matrix.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "Requirement", "Derived from", "Satisfied by", "Verified by", "Status"])
        w.writerows(ch.coverage_rows)
    count = 0
    for title, blocks in ch.sections:
        current = title
        for b in blocks:
            if b[0] == "h3":
                current = b[1]
            if b[0] == "mermaid":
                count += 1
                slug = re.sub(r"[^A-Za-z0-9]+", "_", current).strip("_").lower()
                with open(os.path.join(out_dir, "diagrams", f"{count:02d}_{slug}.mmd"), "w",
                          encoding="utf-8") as fh:
                    fh.write(b[1] + "\n")
    return count


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Check a SysML v2 textual model and generate views.")
    ap.add_argument("model", help="path to the .sysml file")
    ap.add_argument("--out", default=None, help="output folder (default: reports next to the model)")
    args = ap.parse_args(argv)
    try:
        with open(args.model, encoding="utf-8-sig") as fh:
            text = fh.read()
        model = parse_model(text)
    except (OSError, ParseError) as e:
        print(f"Could not read the model: {e}")
        return 2
    ch = Checker(model).run()
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(args.model)), "reports")
    n = write_outputs(ch, out_dir, os.path.basename(args.model))
    errs = [f for f in ch.findings if f.level == "ERROR"]
    warns = [f for f in ch.findings if f.level == "WARNING"]
    print(f"Model: {model.root_name}  ({len(model.requirements)} requirements, "
          f"{len(model.verifications)} verification activities)")
    print(f"Result: {plural(len(errs), 'error')}, {plural(len(warns), 'warning')}")
    for f in errs + warns:
        print(f"  {f.level:7} {f.check}: {f.message}")
    print(f"Report: {os.path.join(out_dir, 'model_report.html')} (+ .md, coverage_matrix.csv, "
          f"{n} diagrams)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
