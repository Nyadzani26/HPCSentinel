"""Which node sits in which chassis, so the Overview can show them in the order they stand in the rack.

This is display only. It never changes how a node is scored: that depends on the node's peer group (NodeGroup), and
moving nodes between chassis leaves every baseline and every evaluation result as it was.

The administrator describes the layout as text, one chassis per line:

    Chassis 1: compute01-compute12
    Chassis 2: compute13-compute24, compute30

A range needs the same name prefix at both ends and keeps the zero padding (compute01-compute12 or compute01-12).
Saving replaces the whole layout, so a node that is not listed has no chassis.
"""
import re

from django.db import transaction

from core.models import Chassis, Node

MAX_RANGE = 1000
RANGE = re.compile(r"^(?P<p1>\D*?)(?P<a>\d+)\s*[-–]\s*(?P<p2>\D*?)(?P<b>\d+)$")
NAME = re.compile(r"^(?P<prefix>\D*?)(?P<num>\d+)$")


class LayoutError(ValueError):
    """The text could not be understood; the message says what to change."""


def natural_key(text: str):
    """Sort 'Chassis 2' before 'Chassis 10'."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def _expand(item: str, known: set) -> list[str]:
    if item in known:                                    # a real node name wins, even if it looks like a range
        return [item]
    m = RANGE.match(item)
    if not m:
        return [item]
    first, last = m["p1"], m["p2"]
    if last and last != first:
        raise LayoutError(f"'{item}' mixes two different name beginnings ('{first}' and '{last}').")
    low, high = int(m["a"]), int(m["b"])
    if low > high:
        raise LayoutError(f"'{item}' runs backwards.")
    if high - low >= MAX_RANGE:
        raise LayoutError(f"'{item}' is longer than {MAX_RANGE} nodes.")
    width = len(m["a"])
    return [f"{first}{n:0{width}d}" for n in range(low, high + 1)]


def parse(text: str, known_names) -> dict[str, list[str]]:
    """Turn the text into {chassis name: [node names]}, or raise LayoutError saying what is wrong."""
    known = set(known_names)
    layout: dict[str, list[str]] = {}
    placed: dict[str, str] = {}
    unknown: list[str] = []
    for number, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, colon, rest = line.partition(":")
        name, rest = name.strip(), rest.strip()
        if not colon or not name or not rest:
            raise LayoutError(f"Line {number}: write it like 'Chassis 1: compute01-compute12'.")
        if len(name) > 60:
            raise LayoutError(f"Line {number}: the chassis name is longer than 60 characters.")
        if name in layout:
            raise LayoutError(f"Line {number}: '{name}' is listed twice. Put all its nodes on one line.")
        nodes: list[str] = []
        for item in (part.strip() for part in rest.split(",")):
            if not item:
                continue
            for node in _expand(item, known):
                if node not in known:
                    if node not in unknown:
                        unknown.append(node)
                elif node in placed:
                    raise LayoutError(f"{node} is in both '{placed[node]}' and '{name}'. A node can be in one chassis.")
                else:
                    placed[node] = name
                    nodes.append(node)
        layout[name] = nodes
    if unknown:
        shown = ", ".join(unknown[:8]) + (f" and {len(unknown) - 8} more" if len(unknown) > 8 else "")
        raise LayoutError(f"The platform does not know these nodes: {shown}. Check the spelling against the node list on the Overview.")
    return layout


@transaction.atomic
def apply(layout: dict[str, list[str]]) -> int:
    """Make this the layout: the chassis in it exist, every node in it is in its chassis, every other node has none."""
    Node.objects.update(chassis=None)
    Chassis.objects.exclude(name__in=list(layout)).delete()
    for name, nodes in layout.items():
        chassis, _ = Chassis.objects.get_or_create(name=name)
        Node.objects.filter(name__in=nodes).update(chassis=chassis)
    return sum(len(nodes) for nodes in layout.values())


def _compress(names: list[str]) -> str:
    """compute01, compute02, compute03 -> compute01-compute03. Runs of fewer than three stay as single names."""
    out: list[str] = []
    run: list[tuple[str, tuple | None]] = []

    def close():
        out.extend([f"{run[0][0]}-{run[-1][0]}"] if len(run) >= 3 else [name for name, _ in run])
        run.clear()

    for name in names:
        m = NAME.match(name)
        key = (m["prefix"], len(m["num"]), int(m["num"])) if m else None
        previous = run[-1][1] if run else None
        if key and previous and previous[:2] == key[:2] and key[2] == previous[2] + 1:
            run.append((name, key))
        else:
            if run:
                close()
            run.append((name, key))
    if run:
        close()
    return ", ".join(out)


def layout_text() -> str:
    """The saved layout in the same form the administrator types it, to fill in the box."""
    chassis = sorted(Chassis.objects.all(), key=lambda c: natural_key(c.name))
    return "\n".join(f"{c.name}: {_compress(sorted(c.nodes.values_list('name', flat=True), key=natural_key))}" for c in chassis)
