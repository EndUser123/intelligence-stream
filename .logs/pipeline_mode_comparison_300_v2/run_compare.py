from pathlib import Path
import sys

sys.path.insert(0, r"P:\\\\\\packages\yt-is")

from csf.breadth_series import BreadthTier, run_pipeline_mode_comparison


def main() -> int:
    report = run_pipeline_mode_comparison(
        trace_root=Path(r"$CLAUDE_PLUGIN_ROOT/.logs\worker_count_trials"),
        output_root=Path(r"$CLAUDE_PLUGIN_ROOT/.logs\pipeline_mode_comparison_300_v2"),
        workers=4,
        phase_b_workers=(),
        limit=300,
        batch_size=200,
        tiers=(BreadthTier("narrow", "Narrow cohort", "captioned", "breadth_narrow"),),
    )
    print(report["report_path"])
    print(report["winner"]["reusable_pipeline_mode"])
    print(report["winner"]["phase_a"]["winner"]["videos_per_hour"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
