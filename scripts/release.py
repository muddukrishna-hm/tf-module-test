#!/usr/bin/env python3
"""Version and release every changed Terraform module (POC)."""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request

MODULE_ROOT = "aws/tf-modules"
RANK = {"patch": 1, "minor": 2, "major": 3}
SENSITIVE_TYPES = {
    "aws_secretsmanager_secret", "aws_secretsmanager_secret_version", "random_password",
    "aws_kms_key", "aws_kms_alias", "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_s3_bucket", "aws_iam_role", "aws_iam_policy", "aws_iam_role_policy",
    "aws_iam_role_policy_attachment", "aws_security_group", "aws_security_group_rule",
    "aws_eks_cluster", "aws_eks_node_group",
}
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
COMMIT_RE = re.compile(r"^(?:\[?[A-Z]+-\d+\]?:?\s*)?([a-z]+)(\([^)]*\))?(!)?:")


def git(*args, check=True, env=None):
    r = subprocess.run(["git", *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def auth_env(token):
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}", "GIT_TERMINAL_PROMPT": "0"})
    return env


def repo_slug():
    url = git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not m:
        sys.exit(f"origin is not a GitHub repo: {url}")
    return m.group(1)


def create_release(token, slug, tag, sha, body):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/releases",
        data=json.dumps({"tag_name": tag, "target_commitish": sha, "name": tag, "body": body}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["html_url"]


def maxb(*bumps):
    return max(bumps, key=lambda b: RANK[b])


def last_tag(module):
    found = []
    for tag in git("tag", "-l", f"{module}/v*").splitlines():
        m = TAG_RE.match(tag[len(module) + 1:])
        if m:
            found.append((tuple(int(x) for x in m.groups()), tag))
    return max(found) if found else (None, None)


def next_version(last, bump):
    if last is None:
        return "v1.0.0"
    ma, mi, pa = last
    return {"major": f"v{ma + 1}.0.0", "minor": f"v{ma}.{mi + 1}.0"}.get(bump, f"v{ma}.{mi}.{pa + 1}")


def parse_module(ref, module):
    merged = {}
    for f in git("ls-tree", "--name-only", ref, f"{MODULE_ROOT}/{module}/", check=False).splitlines():
        if not f.endswith(".tf"):
            continue
        r = subprocess.run(["hcl2json"], input=git("show", f"{ref}:{f}"), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"hcl2json failed on {f}: {r.stderr.strip()}")
        for k, v in json.loads(r.stdout or "{}").items():
            if isinstance(v, list):
                merged.setdefault(k, []).extend(v)
            elif k == "resource":
                for rtype, names in v.items():
                    merged.setdefault("resource", {}).setdefault(rtype, {}).update(names)
            else:
                merged.setdefault(k, {}).update(v)
    return merged


def unwrap(v):
    return v[2:-1] if isinstance(v, str) and v.startswith("${") and v.endswith("}") else v


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
        b, required = blocks[0], "default" not in blocks[0]
        if name not in ov:
            bump = maxb(bump, "major" if required else "minor")
            reasons.append(f"new {'required' if required else 'optional'} variable: {name}")
            continue
        ob = ov[name][0]
        if required and "default" in ob:
            bump = maxb(bump, "major"); reasons.append(f"variable became required: {name}")
        if unwrap(ob.get("type")) != unwrap(b.get("type")):
            bump = maxb(bump, "major"); reasons.append(f"variable type changed: {name}")
        if ob.get("validation") != b.get("validation"):
            bump = maxb(bump, "major"); reasons.append(f"variable validation changed: {name}")
    oo, no = old.get("output", {}), new.get("output", {})
    for name in oo.keys() - no.keys():
        bump = maxb(bump, "major"); reasons.append(f"output removed/renamed: {name}")
    for name in no.keys() - oo.keys():
        bump = maxb(bump, "minor"); reasons.append(f"new output: {name}")

    def constraints(doc):
        tf = {}
        for block in doc.get("terraform", []):
            tf["required_version"] = block.get("required_version", tf.get("required_version"))
            for rp in block.get("required_providers", []):
                tf.setdefault("required_providers", {}).update(rp)
        return tf
    if constraints(old) != constraints(new):
        bump = maxb(bump, "major"); reasons.append("required_version / required_providers changed")
    return bump, reasons


def classify_resources(old, new):
    def res(doc):
        return {f"{t}.{n}": b[0] for t, names in doc.get("resource", {}).items() for n, b in names.items()}
    bump, reasons, review = "patch", [], False
    orr, nr = res(old), res(new)
    moved_from = {unwrap(m.get("from")) for m in new.get("moved", [])}
    moved_to = {unwrap(m.get("to")) for m in new.get("moved", [])}
    for addr in orr.keys() - nr.keys():
        if addr in moved_from:
            reasons.append(f"resource renamed with moved block: {addr}")
        else:
            bump = maxb(bump, "major"); reasons.append(f"resource removed/renamed without moved block: {addr}")
        review |= addr.split(".")[0] in SENSITIVE_TYPES
    for addr in nr.keys() - orr.keys() - moved_to:
        bump = maxb(bump, "minor"); reasons.append(f"new resource: {addr}")
        review |= addr.split(".")[0] in SENSITIVE_TYPES
    for addr in orr.keys() & nr.keys():
        for meta in ("count", "for_each"):
            if orr[addr].get(meta) != nr[addr].get(meta):
                bump = maxb(bump, "major"); reasons.append(f"{meta} changed on {addr}")
        if addr.split(".")[0] in SENSITIVE_TYPES and orr[addr] != nr[addr]:
            review = True
    if review:
        reasons.append("sensitive resource changed: review required")
    return bump, reasons, review


def main():
    token = os.environ.get("GIT_TEST_TOKEN")
    if not token:
        sys.exit("GIT_TEST_TOKEN is not set")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        sys.exit("run on main")
    env, slug = auth_env(token), repo_slug()

    git("fetch", "--tags", "--prune-tags", "origin", env=env)
    git("push", "origin", "main", env=env)
    sha = git("rev-parse", "HEAD")
    modules = [l.strip() for l in open("modules.txt") if l.strip()]
    print(f"repo {slug}  commit {sha[:7]}")

    released = 0
    for module in modules:
        last_v, last_ref = last_tag(module)
        if last_ref and not git("diff", "--name-only", f"{last_ref}..{sha}", "--", f"{MODULE_ROOT}/{module}"):
            print(f"  {module:<26} unchanged ({last_ref})")
            continue
        old = parse_module(last_ref, module) if last_ref else {}
        new = parse_module(sha, module)
        c, cr = classify_commits(last_ref, sha, module)
        i, ir = classify_interface(old, new)
        r, rr, review = classify_resources(old, new)
        bump = maxb(c, i, r)
        tag = f"{module}/{next_version(last_v, bump)}"
        reasons = cr + ir + rr or ["values or internal logic changed only"]
        rng = f"{last_ref}..{sha}" if last_ref else sha
        commits = git("log", "--no-merges", "--format=- %s (%h)", rng, "--", f"{MODULE_ROOT}/{module}")
        body = "\n".join([
            f"**Bump:** {'initial' if not last_v else bump} (commit={c}, interface={i}, resources={r})",
            f"**Status:** {'review-required' if review else 'auto'}",
            f"**Previous:** {last_ref or 'none'}", "", "**Detected changes**",
            *[f"- {x}" for x in reasons], "", "**Commits**", commits or "- none",
        ])
        url = create_release(token, slug, tag, sha, body)
        released += 1
        flag = "  [review-required]" if review else ""
        print(f"  {module:<26} {last_ref or '-'} -> {tag}  ({'initial' if not last_v else bump}){flag}\n      {url}")

    git("fetch", "--tags", "origin", env=env)
    print(f"released {released} module(s)")


if __name__ == "__main__":
    main()
