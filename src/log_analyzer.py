"""
Log analysis tool for understanding EWS MCP Server behavior.
Provides insights, patterns, and issue detection from logs.
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta


class LogAnalyzer:
    """Analyze logs for patterns, issues, and insights."""

    def __init__(self, log_dir: Path = Path("/app/logs")):
        """Initialize the log analyzer.

        Args:
            log_dir: Directory containing log files
        """
        self.log_dir = log_dir

    def read_logs(self,
                  log_file: str,
                  since: Optional[datetime] = None,
                  level: Optional[str] = None,
                  limit: Optional[int] = None) -> List[Dict]:
        """Read and parse log entries.

        Args:
            log_file: Name of log file to read
            since: Only return entries after this datetime
            level: Filter by log level
            limit: Maximum number of entries to return

        Returns:
            List of log entry dictionaries
        """
        log_path = self.log_dir / log_file
        if not log_path.exists():
            return []

        entries = []
        with open(log_path, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())

                    # Filter by timestamp
                    if since:
                        entry_time = datetime.fromisoformat(entry['timestamp'])
                        if entry_time < since:
                            continue

                    # Filter by level
                    if level and entry.get('level') != level:
                        continue

                    entries.append(entry)

                    # Check limit
                    if limit and len(entries) >= limit:
                        break

                except json.JSONDecodeError:
                    continue

        return entries

    def get_error_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get summary of errors in last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            Dictionary with error summary and breakdown
        """
        since = datetime.now() - timedelta(hours=hours)
        errors = self.read_logs("ews_mcp_errors.log", since=since)

        # Group by error type
        error_types = {}
        for error in errors:
            result = error.get('result', {})
            error_type = result.get('error_type', 'Unknown')

            if error_type not in error_types:
                error_types[error_type] = []

            error_types[error_type].append(error)

        return {
            "total_errors": len(errors),
            "time_range_hours": hours,
            "error_types": {
                error_type: {
                    "count": len(instances),
                    "examples": instances[:3],  # First 3 examples
                    "latest": instances[-1] if instances else None
                }
                for error_type, instances in error_types.items()
            }
        }

    def get_performance_metrics(self, hours: int = 24) -> Dict[str, Any]:
        """Get performance metrics for last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            Dictionary with performance metrics by tool
        """
        since = datetime.now() - timedelta(hours=hours)
        metrics = self.read_logs("ews_mcp_performance.log", since=since)

        # Group by tool
        tool_metrics = {}
        for metric in metrics:
            if metric.get('metric') == 'api_call':
                tool = metric.get('tool', 'unknown')
                if tool not in tool_metrics:
                    tool_metrics[tool] = {
                        "calls": 0,
                        "total_duration_ms": 0,
                        "durations": [],
                        "success": 0,
                        "failed": 0
                    }

                tool_metrics[tool]["calls"] += 1
                duration = metric.get('duration_ms', 0)
                tool_metrics[tool]["total_duration_ms"] += duration
                tool_metrics[tool]["durations"].append(duration)

                if metric.get('status') == 'success':
                    tool_metrics[tool]["success"] += 1
                else:
                    tool_metrics[tool]["failed"] += 1

        # Calculate averages and percentiles
        for tool, m in tool_metrics.items():
            if m["calls"] > 0:
                m["avg_duration_ms"] = m["total_duration_ms"] / m["calls"]
                m["success_rate"] = m["success"] / m["calls"]

                # Calculate percentiles
                sorted_durations = sorted(m["durations"])
                n = len(sorted_durations)
                m["p50_duration_ms"] = sorted_durations[n // 2] if n > 0 else 0
                m["p95_duration_ms"] = sorted_durations[int(n * 0.95)] if n > 0 else 0
                m["p99_duration_ms"] = sorted_durations[int(n * 0.99)] if n > 0 else 0

            # Don't need raw durations list in output
            del m["durations"]

        return {
            "time_range_hours": hours,
            "tools": tool_metrics
        }

    def get_test_summary(self) -> Dict[str, Any]:
        """Get summary of test executions.

        Returns:
            Dictionary with test results summary
        """
        tests = self.read_logs("ews_mcp_test_results.log")

        if not tests:
            return {
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0,
                "test_suites": {}
            }

        passed = sum(1 for t in tests if t.get('status') == 'PASSED')
        failed = sum(1 for t in tests if t.get('status') == 'FAILED')
        errors = sum(1 for t in tests if t.get('status') == 'ERROR')

        # Group by test suite
        suites = {}
        for test in tests:
            suite = test.get('test_suite', 'unknown')
            if suite not in suites:
                suites[suite] = {
                    "passed": 0,
                    "failed": 0,
                    "errors": 0,
                    "tests": []
                }

            status = test.get('status')
            if status == 'PASSED':
                suites[suite]["passed"] += 1
            elif status == 'FAILED':
                suites[suite]["failed"] += 1
            elif status == 'ERROR':
                suites[suite]["errors"] += 1

            suites[suite]["tests"].append(test)

        # Calculate suite pass rates
        for suite, data in suites.items():
            total = data["passed"] + data["failed"] + data["errors"]
            data["pass_rate"] = data["passed"] / total if total > 0 else 0

        return {
            "total_tests": len(tests),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": passed / len(tests),
            "test_suites": suites
        }

    def get_activity_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get activity summary for last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            Dictionary with activity summary
        """
        since = datetime.now() - timedelta(hours=hours)
        activities = self.read_logs("ews_mcp_activity.log", since=since)

        # Group by action
        action_counts = {}
        module_counts = {}

        for activity in activities:
            action = activity.get('action', 'unknown')
            module = activity.get('module', 'unknown')

            action_counts[action] = action_counts.get(action, 0) + 1
            module_counts[module] = module_counts.get(module, 0) + 1

        return {
            "time_range_hours": hours,
            "total_activities": len(activities),
            "actions": action_counts,
            "modules": module_counts
        }

    def generate_summary_report(self) -> str:
        """Generate human-readable summary for Claude or users.

        Returns:
            Formatted summary report string
        """
        errors = self.get_error_summary(hours=24)
        performance = self.get_performance_metrics(hours=24)
        tests = self.get_test_summary()
        activity = self.get_activity_summary(hours=24)

        report = f"""
# EWS MCP Server - Log Analysis Report
Generated: {datetime.now().isoformat()}

## Activity Summary (Last 24 Hours)
Total Activities: {activity['total_activities']}

Top Actions:
"""

        # Top 10 actions
        sorted_actions = sorted(activity['actions'].items(), key=lambda x: x[1], reverse=True)[:10]
        for action, count in sorted_actions:
            report += f"  - {action}: {count}\n"

        report += f"""
## Error Summary (Last 24 Hours)
Total Errors: {errors['total_errors']}

Error Breakdown:
"""

        for error_type, data in errors['error_types'].items():
            report += f"  - {error_type}: {data['count']} occurrences\n"
            if data['latest']:
                latest_time = data['latest'].get('timestamp', 'unknown')
                report += f"    Latest: {latest_time}\n"

        report += f"""
## Performance Metrics (Last 24 Hours)

Tool Performance:
"""

        for tool, metrics in performance['tools'].items():
            report += f"""  - {tool}:
      Calls: {metrics['calls']}
      Avg Duration: {metrics['avg_duration_ms']:.0f}ms
      P95 Duration: {metrics['p95_duration_ms']:.0f}ms
      Success Rate: {metrics['success_rate']:.1%}
"""

        if tests['total_tests'] > 0:
            report += f"""
## Test Results
Total Tests: {tests['total_tests']}
Passed: {tests['passed']} ({tests['pass_rate']:.1%})
Failed: {tests['failed']}
Errors: {tests['errors']}

Test Suites:
"""

            for suite, data in tests['test_suites'].items():
                total = data['passed'] + data['failed'] + data['errors']
                report += f"  - {suite}: {data['passed']}/{total} passed ({data['pass_rate']:.1%})\n"

        return report

    def find_slow_operations(self, threshold_ms: int = 2000, hours: int = 24) -> List[Dict]:
        """Find operations that took longer than threshold.

        Args:
            threshold_ms: Duration threshold in milliseconds
            hours: Number of hours to look back

        Returns:
            List of slow operations
        """
        since = datetime.now() - timedelta(hours=hours)
        metrics = self.read_logs("ews_mcp_performance.log", since=since)

        slow_ops = [
            m for m in metrics
            if m.get('duration_ms', 0) > threshold_ms
        ]

        # Sort by duration
        slow_ops.sort(key=lambda x: x.get('duration_ms', 0), reverse=True)

        return slow_ops

    def find_recurring_errors(self, min_count: int = 3, hours: int = 24) -> Dict[str, Any]:
        """Find errors that occur multiple times.

        Args:
            min_count: Minimum occurrences to be considered recurring
            hours: Number of hours to look back

        Returns:
            Dictionary of recurring errors
        """
        error_summary = self.get_error_summary(hours=hours)

        recurring = {
            error_type: data
            for error_type, data in error_summary['error_types'].items()
            if data['count'] >= min_count
        }

        return {
            "time_range_hours": hours,
            "recurring_errors": recurring,
            "total_recurring_types": len(recurring)
        }
