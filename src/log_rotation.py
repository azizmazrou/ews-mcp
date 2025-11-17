"""
Log rotation to prevent disk space issues.
Automatically archives old logs and removes outdated archives.
"""

from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any
import shutil
import gzip
import logging

logger = logging.getLogger(__name__)


def rotate_logs(log_dir: Path = Path("/app/logs"), keep_days: int = 30):
    """Rotate old logs to daily archives.

    Args:
        log_dir: Directory containing log files
        keep_days: Number of days to keep archived logs
    """
    today = datetime.now().date()
    daily_dir = log_dir / "daily"
    daily_dir.mkdir(exist_ok=True)

    # Archive files older than today
    log_files = list(log_dir.glob("*.log"))
    archived_count = 0

    for log_file in log_files:
        try:
            # Check if file was modified today
            file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime).date()

            if file_mtime < today:
                # Archive with date prefix and gzip compression
                archive_name = daily_dir / f"{file_mtime.isoformat()}_{log_file.name}.gz"

                # Compress and archive
                with open(log_file, 'rb') as f_in:
                    with gzip.open(archive_name, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)

                # Remove original file after successful archiving
                log_file.unlink()
                archived_count += 1
                logger.info(f"Archived {log_file.name} to {archive_name}")

        except Exception as e:
            logger.error(f"Failed to archive {log_file}: {e}")

    # Delete old archives
    cutoff_date = today - timedelta(days=keep_days)
    deleted_count = 0

    for archive in daily_dir.glob("*.log.gz"):
        try:
            # Extract date from filename (format: YYYY-MM-DD_filename.log.gz)
            date_str = archive.stem.split('_')[0]
            file_date = datetime.fromisoformat(date_str).date()

            if file_date < cutoff_date:
                archive.unlink()
                deleted_count += 1
                logger.info(f"Deleted old archive: {archive.name}")

        except (ValueError, IndexError) as e:
            logger.warning(f"Could not parse date from archive {archive.name}: {e}")
        except Exception as e:
            logger.error(f"Failed to delete archive {archive}: {e}")

    logger.info(f"Log rotation complete: archived {archived_count} files, deleted {deleted_count} old archives")

    return {
        "archived": archived_count,
        "deleted": deleted_count,
        "archive_dir": str(daily_dir)
    }


def get_disk_usage(log_dir: Path = Path("/app/logs")) -> Dict[str, Any]:
    """Get disk usage statistics for log directory.

    Args:
        log_dir: Directory containing log files

    Returns:
        Dictionary with disk usage stats
    """
    total_size = 0
    file_count = 0
    log_sizes = {}

    # Current logs
    for log_file in log_dir.glob("*.log"):
        size = log_file.stat().st_size
        total_size += size
        file_count += 1
        log_sizes[log_file.name] = size

    # Archived logs
    daily_dir = log_dir / "daily"
    if daily_dir.exists():
        for archive in daily_dir.glob("*.log.gz"):
            size = archive.stat().st_size
            total_size += size
            file_count += 1
            log_sizes[archive.name] = size

    return {
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "file_count": file_count,
        "log_sizes": log_sizes
    }


if __name__ == "__main__":
    # Can be run standalone for manual rotation
    logging.basicConfig(level=logging.INFO)
    result = rotate_logs()
    print(f"Rotation complete: {result}")

    usage = get_disk_usage()
    print(f"Disk usage: {usage['total_size_mb']}MB across {usage['file_count']} files")
