from collections import Counter


def normalize_directory(directory: str) -> str:
    """Clean up what the LLM typed: './src/' and 'src' both become 'src'."""
    d = directory.strip().replace("\\", "/")
    if d.startswith("./"):
        d = d[2:]
    return d.strip("/")


def build_outline(paths: list[str], max_root_files: int = 40) -> str:
    """A short overview for the prompt: top-level files, and top-level folders with file counts."""
    root_files = sorted(p for p in paths if "/" not in p)
    folders = Counter(p.split("/", 1)[0] for p in paths if "/" in p)

    shown = root_files[:max_root_files]
    first_line = "Files in the top level: " + (", ".join(shown) if shown else "(none)")
    if len(root_files) > len(shown):
        first_line += f", ... and {len(root_files) - len(shown)} more"

    lines = [first_line, "Folders (number of files inside, including subfolders):"]
    lines += [f"  {name}/ ({folders[name]} files)" for name in sorted(folders)]
    return "\n".join(lines)


def list_directory_entries(paths: list[str], directory: str, max_entries: int) -> str | None:
    """What is directly inside one folder. Returns None if the folder does not exist."""
    d = normalize_directory(directory)
    prefix = f"{d}/" if d else ""

    subfolders: Counter[str] = Counter()
    files: list[str] = []
    for p in paths:
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if "/" in rest:
            subfolders[rest.split("/", 1)[0]] += 1
        else:
            files.append(rest)

    if not subfolders and not files:
        return None

    entries = [f"{name}/ ({subfolders[name]} files)" for name in sorted(subfolders)] + sorted(files)
    text = "\n".join(entries[:max_entries])
    if len(entries) > max_entries:
        text += f"\n... and {len(entries) - max_entries} more entries"
    return text