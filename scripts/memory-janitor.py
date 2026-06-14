#!/usr/bin/env python3
"""
Memory Janitor - Auto-archive expired memories

Based on @ohxiyu's memory management system:
- P0: Core identity, never expire
- P1: Active projects, 90-day TTL
- P2: Temporary, 30-day TTL
- Promoted from Short-Term Memory: treated as P2 (30-day TTL from promotion date)

Usage:
  python3 memory-janitor.py              # Run archival
  python3 memory-janitor.py --dry-run    # Preview only
  python3 memory-janitor.py --stats      # Show statistics

Changelog:
  v3 (2026-06-13): Added promoted-from-short-term-memory section handling (30-day P2 TTL)
  v2 (2026-02-10): Added atomic write, MAX_LINES warning, dedup archive, line-start regex
"""

import re
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path.home() / ".openclaw/workspace"
MEMORY_FILE = WORKSPACE / "MEMORY.md"
ARCHIVE_DIR = WORKSPACE / "memory/archive"
MAX_LINES = 200
P1_TTL_DAYS = 90
P2_TTL_DAYS = 30
PROMOTED_TTL_DAYS = 30  # Promoted content treated as P2

# Pattern: Lines starting with "- [P0]" or "- [P1][2026-02-10]" etc.
# Must be at line start (after optional whitespace) to avoid matching inline references
PRIORITY_PATTERN = re.compile(r'^\s*-\s*\[P([012])\](?:\[(\d{4}-\d{2}-\d{2})\])?')

# Pattern: "## Promoted From Short-Term Memory (YYYY-MM-DD)" section header
PROMOTED_SECTION_HEADER = re.compile(
    r'^##\s+Promoted\s+From\s+Short-Term\s+Memory\s+\((\d{4}-\d{2}-\d{2})\)\s*$'
)

# Pattern: promotion comment marker "<!-- openclaw-memory-promotion:... -->"
PROMOTION_COMMENT = re.compile(r'^<!--\s*openclaw-memory-promotion:')


def parse_line(line):
    """Extract priority and date from a line. Only matches bullet points at line start."""
    match = PRIORITY_PATTERN.match(line)
    if match:
        priority = int(match.group(1))
        date_str = match.group(2)
        if date_str:
            try:
                date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                date = None
        else:
            date = None
        return priority, date
    return None, None


def should_archive(priority, date, today):
    """Determine if a line should be archived based on priority and age."""
    if priority == 0:
        return False  # P0 never expires
    if date is None:
        return False  # No date = keep (can't determine age)

    age_days = (today - date).days

    if priority == 2 and age_days > P2_TTL_DAYS:
        return True
    if priority == 1 and age_days > P1_TTL_DAYS:
        return True

    return False


def should_archive_promoted(promotion_date, today):
    """Promoted content expires after PROMOTED_TTL_DAYS (like P2)."""
    age_days = (today - promotion_date).days
    return age_days > PROMOTED_TTL_DAYS


def atomic_write(path: Path, content: str):
    """Write content to file atomically using temp file + rename."""
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix='.janitor-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(temp_path, path)  # Atomic on POSIX
    except:
        try:
            os.unlink(temp_path)
        except:
            pass
        raise


def load_existing_archive(archive_file: Path) -> set:
    """Load existing archived lines to prevent duplicates."""
    if not archive_file.exists():
        return set()
    return set(archive_file.read_text().splitlines())


def process_promoted_sections(lines, today):
    """Identify and archive expired promoted-from-short-term-memory sections.
    
    Returns (lines_to_keep, lines_to_archive, expired_section_count).
    
    A promoted section consists of:
      ## Promoted From Short-Term Memory (YYYY-MM-DD)
      <comment lines>
      <content lines>
      <blank line separator>
    
    When expired, the entire section (header, comments, content) is archived.
    """
    to_archive = []
    to_keep = []
    promoted_count = 0
    i = 0
    
    while i < len(lines):
        line = lines[i]
        match = PROMOTED_SECTION_HEADER.match(line)
        
        if match:
            section_date_str = match.group(1)
            try:
                section_date = datetime.strptime(section_date_str, "%Y-%m-%d").date()
            except ValueError:
                # Can't parse date, keep the section
                to_keep.append(line)
                i += 1
                continue
            
            # Collect the entire section
            section_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                # A new "## " section header ends the current section
                if next_line.startswith('## '):
                    break
                section_lines.append(next_line)
                i += 1
            
            if should_archive_promoted(section_date, today):
                to_archive.extend(section_lines)
                promoted_count += 1
            else:
                to_keep.extend(section_lines)
        else:
            to_keep.append(line)
            i += 1
    
    return to_keep, to_archive, promoted_count


def run_janitor(dry_run=False, stats_only=False):
    """Main janitor logic."""
    if not MEMORY_FILE.exists():
        print(f"❌ Memory file not found: {MEMORY_FILE}")
        return 1

    today = datetime.now().date()
    lines = MEMORY_FILE.read_text().splitlines()

    # --- Step 1: Process P0/P1/P2 tagged lines ---
    counts = {0: 0, 1: 0, 2: 0, None: 0}
    p_archive = []
    p_keep = []

    for line in lines:
        priority, date = parse_line(line)
        if priority is not None:
            counts[priority] += 1
        else:
            counts[None] += 1

        if should_archive(priority, date, today):
            p_archive.append(line)
        else:
            p_keep.append(line)

    # Reconstruct lines from p_keep (for promoted section processing)
    # We need to work on the full file for promoted sections, then apply p_archive separately
    intermediate = p_keep

    # --- Step 2: Process promoted-from-short-term-memory sections ---
    kept_lines, promo_archive, promo_count = process_promoted_sections(intermediate, today)

    total_to_archive = p_archive + promo_archive

    print("📊 Memory Statistics:")
    print(f"  Total lines: {len(lines)}")
    print(f"  P0 (permanent): {counts[0]}")
    print(f"  P1 (90-day): {counts[1]}")
    print(f"  P2 (30-day): {counts[2]}")
    print(f"  Promoted sections: {promo_count + sum(1 for line in kept_lines if PROMOTED_SECTION_HEADER.match(line))} "
          f"(expired: {promo_count})")
    print(f"  Untagged: {counts[None]}")
    print(f"  To archive (P1/P2): {len(p_archive)}")
    print(f"  To archive (promoted): {len(promo_archive)}")
    print(f"  To keep: {len(kept_lines)}")

    if len(kept_lines) > MAX_LINES:
        print(f"\n⚠️  WARNING: {len(kept_lines)} lines exceeds MAX_LINES ({MAX_LINES})")
        print(f"    Consider reviewing P0 entries or converting some to P1/P2")

    if stats_only:
        return 0

    if not total_to_archive:
        print("✅ Nothing to archive")
        return 0

    print(f"\n📦 Will archive {len(total_to_archive)} lines:")
    for line in total_to_archive[:10]:
        print(f"  - {line[:80]}...")
    if len(total_to_archive) > 10:
        print(f"  ... and {len(total_to_archive) - 10} more")

    if dry_run:
        print("\n🔍 DRY RUN - no changes made")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_file = ARCHIVE_DIR / f"auto-{today.isoformat()}.md"

    existing = load_existing_archive(archive_file)
    new_to_archive = [line for line in total_to_archive if line not in existing]

    if new_to_archive:
        archive_header = f"# Auto-archived {today.isoformat()}\n\n"
        all_archived = list(existing) + new_to_archive
        content_lines = [l for l in all_archived if not l.startswith("# Auto-archived")]
        archive_content = archive_header + "\n".join(content_lines) + "\n"

        atomic_write(archive_file, archive_content)
        print(f"\n✅ Archived {len(new_to_archive)} new lines to {archive_file}")
        if len(total_to_archive) > len(new_to_archive):
            print(f"   ({len(total_to_archive) - len(new_to_archive)} were already archived)")
    else:
        print(f"\n✅ All {len(total_to_archive)} lines already in archive, skipping")

    kept_content = "\n".join(kept_lines) + "\n"
    atomic_write(MEMORY_FILE, kept_content)

    print(f"✅ Memory file now has {len(kept_lines)} lines")

    return 0


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    stats_only = "--stats" in args
    return run_janitor(dry_run=dry_run, stats_only=stats_only)

if __name__ == "__main__":
    sys.exit(main())