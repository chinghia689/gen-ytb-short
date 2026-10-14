from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from prototype.agent.newsroom import run_newsroom_agent
from prototype.feed.rss_reader import fetch_sources, save_records
from prototype.review.queue import create_review_queue, save_review_queue, load_approved_stories
from prototype.scripts.drafts import render_drafts
from prototype.stories.clustering import cluster_articles, load_articles, save_stories
from prototype.performance import DEFAULT_LOG, summarize


BASE_DIR = Path(__file__).resolve().parent


def prepare_review(input_path: Path, stories_path: Path, review_path: Path):
    articles = load_articles(input_path)
    stories = cluster_articles(articles)
    save_stories(stories, stories_path)
    save_review_queue(create_review_queue(stories), review_path)
    return len(articles), stories


def main() -> None:
    parser = argparse.ArgumentParser(description="Bản tin Ngoại hạng Anh tiếng Việt")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="Tải RSS thật, gom story và tạo hàng đợi duyệt")
    fetch.add_argument("--config", type=Path, default=BASE_DIR / "config" / "sources.json")
    fetch.add_argument("--raw-output", type=Path, default=BASE_DIR / "data" / "live_news.json")
    fetch.add_argument("--stories-output", type=Path, default=BASE_DIR / "data" / "stories.json")
    fetch.add_argument("--review-output", type=Path, default=BASE_DIR / "data" / "review_queue.json")

    prepare = commands.add_parser("prepare-review", help="Gom file JSON tin đã có và tạo hàng đợi duyệt")
    prepare.add_argument("--input", type=Path, default=BASE_DIR / "data" / "live_news.json")
    prepare.add_argument("--stories-output", type=Path, default=BASE_DIR / "data" / "stories.json")
    prepare.add_argument("--review-output", type=Path, default=BASE_DIR / "data" / "review_queue.json")

    draft = commands.add_parser("draft", help="Tạo bản nháp chữ overlay và nguồn từ hàng đợi đã duyệt")
    draft.add_argument("--review-input", type=Path, default=BASE_DIR / "data" / "review_queue.json")
    draft.add_argument("--output", type=Path, default=BASE_DIR / "output" / "drafts.md")

    agent = commands.add_parser("agent", help="Agent tự đọc nguồn, tạo chữ ảnh + nhạc và dựng video")
    agent.add_argument("--limit", type=int, default=5, help="Số video cần hoàn tất mỗi vòng (1-10)")
    agent.add_argument("--loop", action="store_true", help="Chạy liên tục theo chu kỳ")
    agent.add_argument("--interval-minutes", type=int, default=120, help="Chu kỳ khi dùng --loop")
    agent.add_argument("--max-stories", type=int, default=None,
                       help="Số tối đa story thử trong một lượt (hữu ích cho benchmark)")
    agent.add_argument("--story-id", default=None,
                       help="Chỉ chạy story này; ép thử lại nếu story đang trong cooldown")

    perf = commands.add_parser("perf", help="Tóm tắt thời gian stage từ performance.jsonl")
    perf.add_argument("--input", type=Path, default=DEFAULT_LOG)
    perf.add_argument("--last", type=int, default=100, help="Số lượt gần nhất để tổng hợp")

    args = parser.parse_args()

    if args.command == "fetch":
        articles, errors = fetch_sources(args.config)
        save_records(articles, args.raw_output)
        _, stories = prepare_review(args.raw_output, args.stories_output, args.review_output)
        candidates = sum(story["competition_candidate"] == "Premier League" for story in stories)
        print(f"Đã lưu {len(articles)} bài RSS thành {len(stories)} story.")
        print(f"{candidates} story là ứng viên Ngoại hạng Anh; tất cả chờ bạn duyệt.")
        print(f"Hàng đợi duyệt: {args.review_output.resolve()}")
        for error in errors:
            print(f"Lỗi nguồn: {error}")
        return

    if args.command == "prepare-review":
        article_count, stories = prepare_review(args.input, args.stories_output, args.review_output)
        print(f"Đã gom {article_count} bài thành {len(stories)} story.")
        print(f"Hàng đợi duyệt: {args.review_output.resolve()}")
        return

    if args.command == "perf":
        print(json.dumps(summarize(args.input, last=max(1, args.last)), ensure_ascii=False, indent=2))
        return

    if args.command == "agent":
        while True:
            result = run_newsroom_agent(
                limit=args.limit,
                max_processed_stories=args.max_stories,
                only_story_id=args.story_id,
            )
            print(f"Agent: {result['completed_videos']}/{result['target_videos']} video hoàn tất; "
                  f"đã xử lý {len(result['processed'])} story từ {result['stories_seen']} story RSS.")
            for item in result["processed"]:
                print(f"{item['status']}: {item['story_id']} — {item.get('title', '')}")
                if item.get("video_path"):
                    print(f"  Video: {item['video_path']}")
                if item.get("reason"):
                    print(f"  Ghi chú: {item['reason']}")
            for error in result["errors"]:
                print(f"Lỗi: {error}")
            if not args.loop:
                break
            time.sleep(max(5, args.interval_minutes) * 60)
        return

    approved, skipped = load_approved_stories(args.review_input)
    render_drafts(approved, args.output)
    print(f"Đã tạo {len(approved)} bản nháp; bỏ qua {len(skipped)} story chưa đủ điều kiện.")
    print(f"Bản nháp: {args.output.resolve()}")
    for reason in skipped[:10]:
        print(f"Chưa đưa vào: {reason}")


if __name__ == "__main__":
    main()
