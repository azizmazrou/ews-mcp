#!/usr/bin/env python3
"""
Interactive log viewer for Claude analysis and troubleshooting.
Provides quick access to log analysis and filtering.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
import sys
import argparse


def view_recent_logs(log_file: str, lines: int = 50, log_dir: Path = Path("/app/logs")):
    """View recent log entries.

    Args:
        log_file: Name of log file to view
        lines: Number of recent lines to show
        log_dir: Directory containing logs
    """
    log_path = log_dir / log_file
    if not log_path.exists():
        print(f"Log file not found: {log_file}")
        return

    print(f"\n=== Last {lines} entries from {log_file} ===\n")

    with open(log_path, 'r') as f:
        all_lines = f.readlines()
        recent = all_lines[-lines:]

        for line in recent:
            try:
                entry = json.loads(line.strip())
                print(format_log_entry(entry))
            except json.JSONDecodeError:
                print(line.strip())


def format_log_entry(entry: dict) -> str:
    """Format log entry for human reading.

    Args:
        entry: Log entry dictionary

    Returns:
        Formatted string
    """
    timestamp = entry.get('timestamp', '')[:19]  # Truncate microseconds
    level = entry.get('level', 'INFO')
    module = entry.get('module', '')
    action = entry.get('action', '')

    result = entry.get('result', {})
    status = result.get('status', '')

    # Color coding for terminal
    level_colors = {
        'ERROR': '\033[91m',  # Red
        'WARNING': '\033[93m',  # Yellow
        'INFO': '\033[92m',  # Green
        'CRITICAL': '\033[95m'  # Magenta
    }
    reset_color = '\033[0m'

    color = level_colors.get(level, '')

    return f"{color}[{timestamp}] [{level:8s}] [{module:15s}] {action:25s} -> {status}{reset_color}"


def search_logs(log_file: str, keyword: str, log_dir: Path = Path("/app/logs")):
    """Search logs for keyword.

    Args:
        log_file: Name of log file to search
        keyword: Keyword to search for
        log_dir: Directory containing logs
    """
    log_path = log_dir / log_file
    if not log_path.exists():
        print(f"Log file not found: {log_file}")
        return

    matches = []

    with open(log_path, 'r') as f:
        for line in f:
            if keyword.lower() in line.lower():
                try:
                    entry = json.loads(line.strip())
                    matches.append(entry)
                except json.JSONDecodeError:
                    pass

    print(f"\n=== Found {len(matches)} matches for '{keyword}' ===\n")
    for entry in matches:
        print(format_log_entry(entry))
        # Print error details if available
        if entry.get('level') in ['ERROR', 'CRITICAL']:
            error = entry.get('result', {}).get('error')
            if error:
                print(f"    Error: {error}")


def show_error_summary(hours: int = 24, log_dir: Path = Path("/app/logs")):
    """Show summary of errors.

    Args:
        hours: Number of hours to look back
        log_dir: Directory containing logs
    """
    log_path = log_dir / "ews_mcp_errors.log"
    if not log_path.exists():
        print("No error log found")
        return

    since = datetime.now() - timedelta(hours=hours)
    errors = []

    with open(log_path, 'r') as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                entry_time = datetime.fromisoformat(entry['timestamp'])
                if entry_time >= since:
                    errors.append(entry)
            except (json.JSONDecodeError, KeyError):
                pass

    # Group by error type
    error_types = {}
    for error in errors:
        error_type = error.get('result', {}).get('error_type', 'Unknown')
        if error_type not in error_types:
            error_types[error_type] = []
        error_types[error_type].append(error)

    print(f"\n=== Error Summary (Last {hours} hours) ===\n")
    print(f"Total Errors: {len(errors)}\n")

    for error_type, instances in sorted(error_types.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"{error_type}: {len(instances)} occurrences")
        if instances:
            latest = instances[-1]
            print(f"  Latest: {latest['timestamp']}")
            print(f"  Message: {latest.get('result', {}).get('error', 'N/A')[:100]}")
            print()


def show_performance_summary(hours: int = 24, log_dir: Path = Path("/app/logs")):
    """Show performance summary.

    Args:
        hours: Number of hours to look back
        log_dir: Directory containing logs
    """
    log_path = log_dir / "ews_mcp_performance.log"
    if not log_path.exists():
        print("No performance log found")
        return

    since = datetime.now() - timedelta(hours=hours)
    metrics = []

    with open(log_path, 'r') as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                entry_time = datetime.fromisoformat(entry['timestamp'])
                if entry_time >= since:
                    metrics.append(entry)
            except (json.JSONDecodeError, KeyError):
                pass

    # Group by tool
    tool_stats = {}
    for metric in metrics:
        if metric.get('metric') == 'api_call':
            tool = metric.get('tool', 'unknown')
            if tool not in tool_stats:
                tool_stats[tool] = {
                    'calls': 0,
                    'total_duration': 0,
                    'success': 0,
                    'failed': 0
                }

            tool_stats[tool]['calls'] += 1
            tool_stats[tool]['total_duration'] += metric.get('duration_ms', 0)

            if metric.get('status') == 'success':
                tool_stats[tool]['success'] += 1
            else:
                tool_stats[tool]['failed'] += 1

    print(f"\n=== Performance Summary (Last {hours} hours) ===\n")

    for tool, stats in sorted(tool_stats.items(), key=lambda x: x[1]['calls'], reverse=True):
        avg_duration = stats['total_duration'] / stats['calls'] if stats['calls'] > 0 else 0
        success_rate = stats['success'] / stats['calls'] if stats['calls'] > 0 else 0

        print(f"{tool}:")
        print(f"  Calls: {stats['calls']}")
        print(f"  Avg Duration: {avg_duration:.0f}ms")
        print(f"  Success Rate: {success_rate:.1%}")
        print()


def main():
    parser = argparse.ArgumentParser(description='EWS MCP Log Viewer')
    parser.add_argument('command', choices=['view', 'search', 'errors', 'performance'],
                       help='Command to execute')
    parser.add_argument('--file', default='activity',
                       choices=['activity', 'performance', 'errors', 'tests', 'audit'],
                       help='Log file to view')
    parser.add_argument('--lines', type=int, default=50,
                       help='Number of lines to show (for view command)')
    parser.add_argument('--keyword', help='Keyword to search for (for search command)')
    parser.add_argument('--hours', type=int, default=24,
                       help='Number of hours to analyze')
    parser.add_argument('--log-dir', type=Path, default=Path('/app/logs'),
                       help='Log directory path')

    args = parser.parse_args()

    log_files = {
        'activity': 'ews_mcp_activity.log',
        'performance': 'ews_mcp_performance.log',
        'errors': 'ews_mcp_errors.log',
        'tests': 'ews_mcp_test_results.log',
        'audit': 'ews_mcp_audit.log'
    }

    if args.command == 'view':
        view_recent_logs(log_files[args.file], args.lines, args.log_dir)

    elif args.command == 'search':
        if not args.keyword:
            print("Error: --keyword required for search command")
            sys.exit(1)
        search_logs(log_files[args.file], args.keyword, args.log_dir)

    elif args.command == 'errors':
        show_error_summary(args.hours, args.log_dir)

    elif args.command == 'performance':
        show_performance_summary(args.hours, args.log_dir)


if __name__ == "__main__":
    main()
