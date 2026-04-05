from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

SOURCE_CANDIDATES = [
    Path("conf/cleaned_deployment.yml"),
    Path("cleaned_deployment.yml"),
]
OUTPUT_FILE = Path("jobs_output.txt")

PRODUCT_BUCKETS = ("EMIO", "EVEC", "REXT", "EMDL")
EMIO: Dict[str, str] = {}
EVEC: Dict[str, str] = {}
REXT: Dict[str, str] = {}
EMDL: Dict[str, str] = {}
OTHER: Dict[str, str] = {}
ALL_JOBS: Dict[str, str] = {}


def load_source() -> Path:
    for path in SOURCE_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("Could not find cleaned_deployment.yml in conf/ or repository root.")


def split_job_blocks(text: str) -> List[str]:
    pattern = re.compile(r"^[ \t]*-\s+name:\s*.+$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    blocks: List[str] = []

    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].rstrip()
        if block:
            blocks.append(block)
    return blocks


def extract_name(block: str) -> str:
    first_line = block.splitlines()[0]
    match = re.search(r"name:\s*(.+)$", first_line)
    if not match:
        raise ValueError(f"Unable to parse job name from: {first_line}")
    raw_name = match.group(1).strip()
    if (raw_name.startswith('"') and raw_name.endswith('"')) or (
        raw_name.startswith("'") and raw_name.endswith("'")
    ):
        return raw_name[1:-1]
    return raw_name


def build_job_key(job_name: str) -> str:
    return re.sub(r"\s+", "_", job_name.strip().lower())


def detect_child_indent(block_lines: List[str]) -> int:
    first_indent = len(block_lines[0]) - len(block_lines[0].lstrip(" "))
    return first_indent + 2


def looks_like_top_level_key(line: str, child_indent: int) -> bool:
    if not line.strip() or line.lstrip().startswith("#"):
        return False

    indent = len(line) - len(line.lstrip(" "))
    if indent != child_indent:
        return False

    stripped = line.strip()
    if stripped.startswith("- "):
        return False
    return ":" in stripped


def extract_section(block_lines: List[str], key: str, child_indent: int) -> str | None:
    start_idx = None
    key_pattern = re.compile(rf"^\s{{{child_indent}}}{re.escape(key)}:\s*(.*)$")

    for idx, line in enumerate(block_lines):
        if key_pattern.match(line):
            start_idx = idx
            break

    if start_idx is None:
        return None

    end_idx = len(block_lines)
    for idx in range(start_idx + 1, len(block_lines)):
        if looks_like_top_level_key(block_lines[idx], child_indent):
            end_idx = idx
            break

    return "\n".join(block_lines[start_idx:end_idx]).rstrip()


def extract_name_level_merges(block_lines: List[str], child_indent: int) -> str | None:
    merge_lines: List[str] = []
    for line in block_lines[1:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == child_indent and re.match(r"^\s*<<:\s*\*[A-Za-z0-9_-]+\s*$", line):
            merge_lines.append(line)
    return "\n".join(merge_lines) if merge_lines else None


def replace_anchor_merges(section: str) -> str:
    return re.sub(
        r"^(\s*)<<:\s*\*([A-Za-z0-9_-]+)\s*$",
        r"\1new_cluster: ${var.custom_clusters.\2}",
        section,
        flags=re.MULTILINE,
    )


def transform_schedule(schedule: str) -> Tuple[str, bool]:
    transformed = schedule
    add_conditional_pause_variables = False

    # quartz_cron_expression: {{ custom.cron_schedule("...", ...) }} -> quartz_cron_expression: "..."
    transformed = re.sub(
        r'^(\s*quartz_cron_expression:\s*)\{\{\s*custom\.cron_schedule\(\s*(["\'])(.*?)\2\s*,.*\)\s*\}\}\s*$',
        lambda m: f'{m.group(1)}"{m.group(3)}"',
        transformed,
        flags=re.MULTILINE,
    )

    transformed = re.sub(
        r"^(\s*pause_status:\s*)\{\{\s*custom\.conditionally_pause_non_prod_job\(.*\)\s*\}\}\s*$",
        r"\1${var.conditionally_pause_non_prod_job}",
        transformed,
        flags=re.MULTILINE,
    )

    transformed = re.sub(
        r"^(\s*pause_status:\s*)\{\{\s*custom\.conditionally_pause_dev_job\(.*\)\s*\}\}\s*$",
        r"\1${var.conditionally_pause_dev_job}",
        transformed,
        flags=re.MULTILINE,
    )

    # pause_status: {{ custom.schedule_pause(..., "PAUSED") }} -> pause_status: "PAUSED"
    transformed = re.sub(
        r'^(\s*pause_status:\s*)\{\{\s*custom\.schedule_pause\([^)]*["\']PAUSED["\'][^)]*\)\s*\}\}\s*$',
        r'\1"PAUSED"',
        transformed,
        flags=re.MULTILINE,
    )

    # pause_status: {{ custom.schedule_pause(..., "UNPAUSED") }} -> pause_status: ${var.conditional_pause_status}
    unpaused_pattern = re.compile(
        r'^(\s*pause_status:\s*)\{\{\s*custom\.schedule_pause\([^)]*["\']UNPAUSED["\'][^)]*\)\s*\}\}\s*$',
        flags=re.MULTILINE,
    )
    if unpaused_pattern.search(transformed):
        add_conditional_pause_variables = True
    transformed = unpaused_pattern.sub(
        r"\1${var.conditional_pause_status}",
        transformed,
    )

    return transformed, add_conditional_pause_variables


def reindent_section(section: str, old_indent: int, new_indent: int) -> str:
    out_lines: List[str] = []
    for line in section.splitlines():
        if not line.strip():
            out_lines.append("")
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        trim = min(current_indent, old_indent)
        out_lines.append((" " * new_indent) + line[trim:])
    return "\n".join(out_lines)


def extract_product_from_tags(tags_section: str | None) -> str:
    if not tags_section:
        return "OTHER"

    match = re.search(
        r'^\s*product:\s*["\']?([A-Za-z0-9_-]+)["\']?\s*$',
        tags_section,
        re.MULTILINE,
    )
    if not match:
        return "OTHER"

    value = match.group(1).strip().upper()
    return value if value in PRODUCT_BUCKETS else "OTHER"


def build_job_yaml(
    job_name: str,
    job_key: str,
    name_level_merges: str | None,
    schedule: str | None,
    job_clusters: str | None,
    tasks: str | None,
    tags: str | None,
    old_indent: int,
    add_conditional_pause_variables: bool,
) -> str:
    lines = [
        "resources:",
        "  jobs:",
        f"    {job_key}:",
        f"      name: {job_name}",
    ]

    if name_level_merges:
        lines.append(reindent_section(name_level_merges, old_indent, 6))

    for section in (schedule, job_clusters, tasks, tags):
        if not section:
            continue
        lines.append(reindent_section(section, old_indent, 6))

    if add_conditional_pause_variables:
        lines.extend(
            [
                "variables:",
                "  dev:",
                '    conditional_pause_status: "PAUSED"',
                "  pre:",
                '    conditional_pause_status: "UNPAUSED"',
                "  prod:",
                '    conditional_pause_status: "UNPAUSED"',
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def parse_jobs(source_text: str) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    product_dicts: Dict[str, Dict[str, str]] = {
        "EMIO": {},
        "EVEC": {},
        "REXT": {},
        "EMDL": {},
        "OTHER": {},
    }
    all_jobs: Dict[str, str] = {}

    for block in split_job_blocks(source_text):
        lines = block.splitlines()
        child_indent = detect_child_indent(lines)
        job_name = extract_name(block)
        job_key = build_job_key(job_name)

        name_level_merges = extract_name_level_merges(lines, child_indent)
        schedule = extract_section(lines, "schedule", child_indent)
        job_clusters = extract_section(lines, "job_clusters", child_indent)
        tasks = extract_section(lines, "tasks", child_indent)
        tags = extract_section(lines, "tags", child_indent)

        add_conditional_pause_variables = False
        if schedule:
            schedule, add_conditional_pause_variables = transform_schedule(schedule)
        if job_clusters:
            job_clusters = replace_anchor_merges(job_clusters)
        if tasks:
            tasks = replace_anchor_merges(tasks)

        job_yaml = build_job_yaml(
            job_name,
            job_key,
            name_level_merges,
            schedule,
            job_clusters,
            tasks,
            tags,
            child_indent,
            add_conditional_pause_variables,
        )
        all_jobs[job_name] = job_yaml

        product_bucket = extract_product_from_tags(tags)
        product_dicts[product_bucket][job_name] = job_yaml

    return product_dicts, all_jobs


def write_output(product_dicts: Dict[str, Dict[str, str]], all_jobs: Dict[str, str]) -> None:
    ordered_buckets = ["EMIO", "EVEC", "REXT", "EMDL", "OTHER"]

    lines = [f"total jobs: {len(all_jobs)}"]
    for bucket in ordered_buckets:
        lines.append(f"{bucket}: {len(product_dicts[bucket])}")

    lines.append("")

    for bucket in ordered_buckets:
        lines.append(f"## {bucket}")
        lines.append("")
        for job_name in sorted(product_dicts[bucket]):
            lines.append(f"### {job_name}")
            lines.append(product_dicts[bucket][job_name].rstrip())
            lines.append("")

    OUTPUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    global EMIO, EVEC, REXT, EMDL, OTHER, ALL_JOBS

    source_path = load_source()
    source_text = source_path.read_text(encoding="utf-8")

    product_dicts, all_jobs = parse_jobs(source_text)
    EMIO = product_dicts["EMIO"]
    EVEC = product_dicts["EVEC"]
    REXT = product_dicts["REXT"]
    EMDL = product_dicts["EMDL"]
    OTHER = product_dicts["OTHER"]
    ALL_JOBS = all_jobs

    write_output(product_dicts, all_jobs)

    print(f"Parsed {len(all_jobs)} jobs from {source_path}.")
    print(
        "Counts -> "
        + ", ".join(
            f"{bucket}: {len(product_dicts[bucket])}"
            for bucket in ["EMIO", "EVEC", "REXT", "EMDL", "OTHER"]
        )
    )
    print(f"Wrote output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
