"""Workflow-script helpers shared (copied) by both repos' tests."""
import os
import re


def job_steps(text, job):
    """[{"name","run","env"}] for the steps of one job in a workflow file.
    Not a YAML parser: enough for these files (a `- ` item per step, `env:`
    as NAME: value lines, `run:` as a `|` block or one line)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln == f"  {job}:")
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith("   "):
            break
        body.append(ln)
    top = next(i for i, ln in enumerate(body) if ln.strip() == "steps:")
    body = body[top + 1:]

    def ind(s):
        return len(s) - len(s.lstrip(" "))

    item = min(ind(ln) for ln in body if ln.lstrip().startswith("- "))
    chunks = []
    for ln in body:
        if ind(ln) == item and ln.lstrip().startswith("- "):
            chunks.append([" " * item + "  " + ln.lstrip()[2:]])
        elif chunks:
            chunks[-1].append(ln)
    steps = []
    for ch in chunks:
        step = {"name": None, "uses": None, "run": None, "env": {},
                "text": "\n".join(ch)}
        ki = ind(ch[0])
        j = 0
        while j < len(ch):
            raw = ch[j]
            j += 1
            m = re.match(r"([A-Za-z_-]+):(?:\s+(.*))?$", raw.strip())
            if ind(raw) != ki or not m:
                continue
            key, val = m.group(1), (m.group(2) or "").strip()
            block = []
            while j < len(ch) and (not ch[j].strip() or ind(ch[j]) > ki):
                block.append(ch[j])
                j += 1
            if key == "name" or key == "uses":
                step[key] = val
            elif key == "env":
                for e in block:
                    k, _, v = e.strip().partition(":")
                    if k and not k.startswith("#"):
                        step["env"][k.strip()] = v.strip()
            elif key == "run":
                if val in ("|", "|-", ">"):
                    first = next((b for b in block if b.strip()), "")
                    step["run"] = "\n".join(
                        b[ind(first):] if b.strip() else "" for b in block
                    ).strip("\n") + "\n"
                else:
                    step["run"] = val
        steps.append(step)
    return steps
