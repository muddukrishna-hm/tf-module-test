#!/usr/bin/env python3
"""POC of the module-release / module-validate logic from the RFC."""
import argparse
import json
import os
import re
import subprocess
import sys

MODULE_ROOT = "aws/tf-modules"
RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}
SENSITIVE_TYPES = {
    "aws_secretsmanager_secret", "aws_secretsmanager_secret_version", "random_password",
    "aws_kms_key", "aws_kms_alias", "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_s3_bucket", "aws_iam_role", "aws_iam_policy", "aws_iam_role_policy",
    "aws_iam_role_policy_attachment", "aws_security_group", "aws_security_group_rule",
    "aws_eks_cluster", "aws_eks_node_group",
}
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
COMMIT_RE = re.compile(r"^(?:\[?[A-Z]+-\d+\]?:?\s*)?([a-z]+)(\([^)]*\))?(!)?:")
RELEASES_DIR = ".releases"


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def maxb(*bumps):
    return max(bumps, key=lambda b: RANK[b])


def read_modules(ref):
    text = git("show", f"{ref}:modules.txt", check=False)
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


def module_versions(module):
    out = []
    for tag in git("tag", "-l", f"{module}/v*").splitlines():
        m = TAG_RE.match(tag[len(module) + 1:])
        if m:
            out.append((tuple(int(x) for x in m.groups()), tag))
    return sorted(out)


def last_tag(module):
    v = module_versions(module)
    return v[-1] if v else (None, None)


def next_version(last, bump):
    if last is None:
        return (1, 0, 0)
    ma, mi, pa = last
    if bump == "major":
        return (ma + 1, 0, 0)
    if bump == "minor":
        return (ma, mi + 1, 0)
    return (ma, mi, pa + 1)


def fmt(v):
    return "v%d.%d.%d" % v


def parse_module(ref, module):
    """Parse all .tf files of a module at a git ref into one merged hcl2json document."""
    path = f"{MODULE_ROOT}/{module}"
    merged = {}
    files = [f for f in git("ls-tree", "--name-only", ref, f"{path}/", check=False).splitlines() if f.endswith(".tf")]
    for f in files:
        src = git("show", f"{ref}:{f}")
        r = subprocess.run(["hcl2json"], input=src, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"hcl2json failed on {ref}:{f}: {r.stderr.strip()}")
        doc = json.loads(r.stdout or "{}")
        for k, v in doc.items():
            if isinstance(v, list):
                merged.setdefault(k, []).extend(v)
            elif k == "resource":
                for rtype, names in v.items():
                    merged.setdefault("resource", {}).setdefault(rtype, {}).update(names)
            else:
                merged.setdefault(k, {}).update(v)
    return merged


def strip_interp(v):
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        return v[2:-1]
    return v


def classify_commits(last_ref, sha, module):
    rng = f"{last_ref}..{sha}" if last_ref else sha
    log = git("log", "--no-merges", "--format=%s%x1f%b%x1e", rng, "--", f"{MODULE_ROOT}/{module}")
    bump, reasons = "patch", []
    for entry in filter(None, (e.strip() for e in log.split("\x1e"))):
        subject, _, body = entry.partition("\x1f")
        m = COMMIT_RE.match(subject)
        if (m and m.group(3)) or "BREAKING CHANGE:" in body:
            bump = maxb(bump, "major"); reasons.append(f"breaking commit: {subject}")
        elif m and m.group(1) == "feat":
            bump = maxb(bump, "minor"); reasons.append(f"feat commit: {subject}")
    return bump, reasons


def classify_interface(old, new):
    bump, reasons = "patch", []
    ov, nv = old.get("variable", {}), new.get("variable", {})
    for name in ov.keys() - nv.keys():
        bump = maxb(bump, "major"); reasons.append(f"variable removed/renamed: {name}")
    for name, blocks in nv.items():
        b = blocks[0]
        required = "default" not in b
        if name not in ov:
            if required:
                bump = maxb(bump, "major"); reasons.append(f"new required variable: {name}")
            else:
                bump = maxb(bump, "minor"); reasons.append(f"new optional variable: {name}")
            continue
        ob = ov[name][0]
        if required and "default" in ob:
            bump = maxb(bump, "major"); reasons.append(f"variable became required: {name}")
        if strip_interp(ob.get("type")) != strip_interp(b.get("type")):
            bump = maxb(bump, "major"); reasons.append(f"variable type changed: {name}")
        if json.dumps(ob.get("validation"), sort_keys=True) != json.dumps(b.get("validation"), sort_keys=True):
            bump = maxb(bump, "major"); reasons.append(f"variable validation changed: {name}")
    oo, no = old.get("output", {}), new.get("output", {})
    for name in oo.keys() - no.keys():
        bump = maxb(bump, "major"); reasons.append(f"output removed/renamed: {name}")
    for name in no.keys() - oo.keys():
        bump = maxb(bump, "minor"); reasons.append(f"new output: {name}")

    def constraints(doc):
        tf = {}
        for block in doc.get("terraform", []):
            if "required_version" in block:
                tf["required_version"] = block["required_version"]
            for rp in block.get("required_providers", []):
                tf.setdefault("required_providers", {}).update(rp)
        return tf
    if constraints(old) != constraints(new):
        bump = maxb(bump, "major"); reasons.append("required_version / required_providers changed")
    return bump, reasons


def resources(doc):
    out = {}
    for rtype, names in doc.get("resource", {}).items():
        for name, blocks in names.items():
            out[f"{rtype}.{name}"] = blocks[0]
    return out


def classify_resources(old, new):
    bump, reasons, review = "patch", [], False
    orr, nr = resources(old), resources(new)
    moved_from = {strip_interp(m.get("from")) for m in new.get("moved", [])}
    for addr in orr.keys() - nr.keys():
        if addr in moved_from:
            reasons.append(f"resource renamed with moved block: {addr}")
        else:
            bump = maxb(bump, "major"); reasons.append(f"resource removed/renamed without moved block: {addr}")
        if addr.split(".")[0] in SENSITIVE_TYPES:
            review = True; reasons.append(f"sensitive resource changed: {addr}")
    for addr in nr.keys() - orr.keys():
        if addr in {strip_interp(m.get("to")) for m in new.get("moved", [])}:
            continue
        bump = maxb(bump, "minor"); reasons.append(f"new resource: {addr}")
        if addr.split(".")[0] in SENSITIVE_TYPES:
            review = True; reasons.append(f"sensitive resource added: {addr}")
    for addr in orr.keys() & nr.keys():
        ob, nb = orr[addr], nr[addr]
        for meta in ("count", "for_each"):
            if ob.get(meta) != nb.get(meta):
                bump = maxb(bump, "major"); reasons.append(f"{meta} changed on {addr}")
        if addr.split(".")[0] in SENSITIVE_TYPES and json.dumps(ob, sort_keys=True) != json.dumps(nb, sort_keys=True):
            review = True; reasons.append(f"sensitive resource changed: {addr}")
    return bump, reasons, review


def build_plan(sha, overrides):
    plan = []
    for module in read_modules(sha):
        last_v, last_ref = last_tag(module)
        path = f"{MODULE_ROOT}/{module}"
        if last_ref:
            changed = git("diff", "--name-only", f"{last_ref}..{sha}", "--", path)
            if not changed:
                continue
        old = parse_module(last_ref, module) if last_ref else {}
        new = parse_module(sha, module)
        c, cr = classify_commits(last_ref, sha, module)
        i, ir = classify_interface(old, new)
        r, rr, review = classify_resources(old, new)
        bump = maxb(c, i, r)
        entry = {
            "module": module, "last": fmt(last_v) if last_v else "-", "last_ref": last_ref,
            "commit": c, "interface": i, "resources": r + (" +review" if review else ""),
            "calculated": bump, "bump": bump, "status": "review-required" if review else "auto",
            "reasons": cr + ir + rr or ["values or internal logic changed only (same interface and resources)"],
            "override": None,
        }
        if module in overrides:
            new_bump, reason = overrides[module]
            entry["override"] = f"{bump} -> {new_bump}: {reason}"
            entry["bump"] = new_bump
        entry["next"] = fmt(next_version(last_v, entry["bump"])) if last_v else "v1.0.0"
        plan.append(entry)
    return plan


def print_plan(plan, sha):
    print(f"\nRELEASE PLAN  (commit {sha[:7]})")
    if not plan:
        print("  no changed modules - nothing to release")
        return
    hdr = f"  {'module':<26}{'last':<9}{'next':<9}{'commit':<8}{'interface':<11}{'resources':<16}{'status'}"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for e in plan:
        print(f"  {e['module']:<26}{e['last']:<9}{e['next']:<9}{e['commit']:<8}{e['interface']:<11}{e['resources']:<16}{e['status']}")
    for e in plan:
        print(f"\n  {e['module']} -> {e['next']} ({e['bump']})")
        for reason in e["reasons"]:
            print(f"    - {reason}")
        if e["override"]:
            print(f"    - OVERRIDE {e['override']}")


def notes_for(e, sha, approver):
    rng = f"{e['last_ref']}..{sha}" if e["last_ref"] else sha
    commits = git("log", "--no-merges", "--format=- %s (%h)", rng, "--", f"{MODULE_ROOT}/{e['module']}")
    lines = [f"{e['module']}/{e['next']}", "", f"Bump: {e['bump']} (commit={e['commit']}, interface={e['interface']}, resources={e['resources']})",
             f"Status: {e['status']}", f"Approved by: {approver}", f"Commit: {sha}"]
    if e["override"]:
        lines.append(f"Override: {e['override']}")
    lines += ["", "Detected changes:"] + [f"- {r}" for r in e["reasons"] or ["- none"]] + ["", "Commits:", commits or "- none"]
    return "\n".join(lines)


def write_release(tag, notes):
    path = os.path.join(RELEASES_DIR, f"{tag}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(notes + "\n")


def repair_releases():
    repaired = []
    for tag in git("tag", "-l").splitlines():
        if "/v" not in tag or os.path.exists(os.path.join(RELEASES_DIR, f"{tag}.md")):
            continue
        body = git("tag", "-l", "--format=%(contents)", tag)
        write_release(tag, body or tag)
        repaired.append(tag)
    return repaired


def parse_overrides(values):
    out = {}
    for v in values or []:
        m = re.match(r'^([^=]+)=(patch|minor|major):(.+)$', v)
        if not m:
            sys.exit(f"invalid --override '{v}' (expected module=bump:reason)")
        out[m.group(1)] = (m.group(2), m.group(3).strip().strip('"'))
    return out


def cmd_release(args):
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        sys.exit("release must run on main")
    sha = git("rev-parse", "HEAD")
    print(f"[1/7] prepare: pinned commit {sha[:7]}")
    overrides = parse_overrides(args.override)
    print("[2/7] detect changed modules  [3/7] classify")
    plan = build_plan(sha, overrides)
    print("[4/7] pre-check + release plan")
    for e in plan:
        tag = f"{e['module']}/{e['next']}"
        if git("tag", "-l", tag):
            sys.exit(f"pre-check failed: tag {tag} already exists")
    print_plan(plan, sha)
    if args.dry_run or not plan:
        print("\nDRY_RUN - nothing created" if args.dry_run else "")
        repaired = repair_releases()
        if repaired:
            print(f"repaired missing releases: {', '.join(repaired)}")
        return
    print("\n[5/7] APPROVAL STAGE")
    if args.approve_as:
        approver = args.approve_as
        print(f"  approved by {approver}")
    else:
        answer = input("  approve this release plan? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit("  rejected - nothing created")
        approver = os.environ.get("USER", "unknown")
    print("[6/7] release")
    released, failed = [], []
    for e in plan:
        tag = f"{e['module']}/{e['next']}"
        try:
            if e["module"] in (args.simulate_failure or []):
                raise RuntimeError("simulated failure")
            notes = notes_for(e, sha, approver)
            git("tag", "-a", tag, sha, "-m", notes)
            write_release(tag, notes)
            released.append(tag)
            print(f"  released {tag}")
        except RuntimeError as ex:
            failed.append(tag)
            print(f"  FAILED   {tag}: {ex}")
    print("[7/7] repair + report")
    repaired = repair_releases()
    if repaired:
        print(f"  repaired missing releases: {', '.join(repaired)}")
    print(f"  released: {len(released)}  failed: {len(failed)}")
    if failed:
        sys.exit(1)


def cmd_validate(args):
    base, head = args.base, git("rev-parse", "HEAD")
    listed = read_modules(head)
    problems = []
    tf_dirs = {os.path.dirname(f)[len(MODULE_ROOT) + 1:] for f in git("ls-tree", "-r", "--name-only", head, MODULE_ROOT).splitlines() if f.endswith(".tf")}
    for d in sorted(tf_dirs):
        if not any(d == m or d.startswith(m + "/") for m in listed):
            problems.append(f"folder not covered by modules.txt: {d}")
    for m in sorted(set(read_modules(base)) - set(listed)):
        problems.append(f"module REMOVED from modules.txt: {m} (warn consumers; its tags are kept)")
    print("module-validate")
    for p in problems:
        print(f"  ! {p}")
    changed = {m for m in listed if git("diff", "--name-only", f"{base}...{head}", "--", f"{MODULE_ROOT}/{m}")}
    plan = [e for e in build_plan(head, {}) if e["module"] in changed]
    print("\nPR comment - expected versions if merged:")
    for e in plan or []:
        print(f"  {e['module']}: {e['last']} -> {e['next']} ({e['bump']}, {e['status']})")
        for r in e["reasons"]:
            print(f"    - {r}")
    if not plan:
        print("  no module changes")
    if any("not covered" in p for p in problems):
        sys.exit(1)


def cmd_baseline(args):
    sha = git("rev-parse", "HEAD")
    for module in read_modules(sha):
        tag = f"{module}/{args.version}"
        if not git("tag", "-l", tag):
            git("tag", "-a", tag, sha, "-m", f"{tag}\n\nBaseline: same code as the shared {args.version} release")
            write_release(tag, f"{tag}\n\nBaseline: same code as the shared {args.version} release")
            print(f"baseline {tag}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("release")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--override", action="append", help='module=bump:"reason"')
    r.add_argument("--approve-as", help="non-interactive approval (simulates the pending-approval step)")
    r.add_argument("--simulate-failure", action="append", help="module to fail during release")
    v = sub.add_parser("validate")
    v.add_argument("--base", default="main")
    b = sub.add_parser("baseline")
    b.add_argument("--version", default="v1.0.24")
    args = p.parse_args()
    {"release": cmd_release, "validate": cmd_validate, "baseline": cmd_baseline}[args.cmd](args)


if __name__ == "__main__":
    main()
