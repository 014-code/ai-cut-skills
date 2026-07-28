from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from douyin_reference_core import (
    VideoReference,
    WanbangClient,
    download_file,
    resolve_references,
    validate_mp4_file,
    write_reference_manifest,
)


@dataclass
class DownloadResult:
    source: str
    gid: str
    video_url: str
    status: str
    keyword: str = ""
    path: str = ""
    file_size: int = 0
    error: str = ""


def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


def text_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_xlsx_column(path: Path, column: str | None) -> list[str]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("Reading .xlsx input requires openpyxl: pip install openpyxl") from exc

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []

    if column:
        column = column.strip()
        if column.isdigit():
            index = max(int(column), 1) - 1
        else:
            headers = [str(value or "").strip() for value in rows[0]]
            if column not in headers:
                raise RuntimeError(f"Column not found in {path}: {column}")
            index = headers.index(column)
            rows = rows[1:]
    else:
        index = 0

    values: list[str] = []
    for row in rows:
        if index < len(row):
            value = str(row[index] or "").strip()
            if value:
                values.append(value)
    return values


def read_csv_column(path: Path, column: str | None) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        sample = file.read(2048)
        file.seek(0)
        has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        if has_header:
            reader = csv.DictReader(file)
            fieldnames = reader.fieldnames or []
            selected = column or (fieldnames[0] if fieldnames else "")
            if selected not in fieldnames:
                raise RuntimeError(f"Column not found in {path}: {selected}")
            return [str(row.get(selected) or "").strip() for row in reader if str(row.get(selected) or "").strip()]

        reader = csv.reader(file)
        index = max(int(column), 1) - 1 if column and column.isdigit() else 0
        values = []
        for row in reader:
            if index < len(row):
                value = str(row[index] or "").strip()
                if value:
                    values.append(value)
        return values


def read_values(path: Path, column: str | None) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx_column(path, column)
    if suffix == ".csv":
        return read_csv_column(path, column)
    return text_lines(path)


def write_outputs(results: list[DownloadResult], references: list[VideoReference], out_dir: Path) -> None:
    write_reference_manifest(references, out_dir / "references.json")
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    csv_path = out_dir / "summary.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(results[0]).keys()) if results else ["status"])
        writer.writeheader()
        for item in results:
            writer.writerow(asdict(item))


def build_references(args: argparse.Namespace, client: WanbangClient | None) -> list[VideoReference]:
    urls = list(args.url or [])
    for path_text in args.urls_file or []:
        urls.extend(read_values(Path(path_text), args.url_column))

    gids = list(args.gid or [])
    for path_text in args.gids_file or []:
        gids.extend(read_values(Path(path_text), args.gid_column))

    keywords = list(args.keyword or [])
    for path_text in args.keywords_file or []:
        keywords.extend(read_values(Path(path_text), args.keyword_column))

    return resolve_references(
        urls=urls,
        gids=gids,
        keywords=keywords,
        client=client,
        page=args.page,
        max_per_keyword=args.max_per_keyword,
        short_url_timeout=args.short_url_timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve and download Douyin references through the shared Douyin toolkit contract."
    )
    parser.add_argument("--out-dir", type=Path, default=Path("downloads/douyin-gid-batch"))
    parser.add_argument("--url", action="append", help="Douyin URL. Can be passed multiple times.")
    parser.add_argument("--urls-file", action="append", help="Text, CSV, or XLSX file containing Douyin URLs.")
    parser.add_argument("--url-column", help="CSV/XLSX URL column name or 1-based index.")
    parser.add_argument("--gid", action="append", help="Raw Douyin GID/aweme id. Can be passed multiple times.")
    parser.add_argument("--gids-file", action="append", help="Text, CSV, or XLSX file containing Douyin GIDs.")
    parser.add_argument("--gid-column", help="CSV/XLSX GID column name or 1-based index.")
    parser.add_argument("--keyword", action="append", help="Keyword to search through Wanbang item_search_video.")
    parser.add_argument("--keywords-file", action="append", help="Text, CSV, or XLSX file containing keywords.")
    parser.add_argument("--keyword-column", help="CSV/XLSX keyword column name or 1-based index.")
    parser.add_argument("--max-per-keyword", type=int, default=12)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--short-url-timeout", type=int, default=20)
    parser.add_argument("--no-download", action="store_true", help="Only resolve/query references and write summaries.")
    parser.add_argument("--skip-existing", action="store_true", help="Reuse only existing <gid>.mp4 files that pass validation.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to wait between videos.")
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    parser.add_argument("--api-key", default=os.getenv("WANBANG_API_KEY", ""))
    parser.add_argument("--api-secret", default=os.getenv("WANBANG_API_SECRET", ""))
    parser.add_argument("--base-url", default=os.getenv("WANBANG_DOUYIN_BASE_URL", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.out_dir / "run.log"
    append_log(
        log_path,
        f"run_start out_dir={args.out_dir.resolve()} no_download={args.no_download} "
        f"skip_existing={args.skip_existing}",
    )
    has_keyword_inputs = bool(args.keyword or args.keywords_file)
    needs_client = has_keyword_inputs or not args.no_download
    try:
        client = (
            WanbangClient(
                args.api_key,
                args.api_secret,
                args.base_url,
                retry_count=args.retry_count,
                retry_delay_seconds=args.retry_delay_seconds,
            )
            if needs_client
            else None
        )
        references = build_references(args, client)
    except Exception as exc:
        append_log(log_path, f"prepare_failed error={exc}")
        raise
    if not references:
        append_log(log_path, "prepare_failed error=No Douyin references found.")
        raise RuntimeError("No Douyin references found.")
    append_log(log_path, f"references_ready count={len(references)}")

    results: list[DownloadResult] = []
    for index, reference in enumerate(references, start=1):
        target = args.out_dir / f"{reference.gid}.mp4" if reference.gid else None
        print(f"[{index}/{len(references)}] {reference.gid or reference.source_url}")
        append_log(
            log_path,
            f"item_start index={index} gid={reference.gid} source={reference.source_url}",
        )
        try:
            if not reference.gid:
                raise RuntimeError(reference.error or "unresolved Douyin reference")
            if args.no_download:
                result = DownloadResult(
                    reference.source_url,
                    reference.gid,
                    reference.video_url,
                    "resolved",
                    keyword=reference.keyword,
                )
            elif args.skip_existing and target is not None and validate_mp4_file(target):
                result = DownloadResult(
                    reference.source_url,
                    reference.gid,
                    reference.video_url,
                    "reused",
                    keyword=reference.keyword,
                    path=str(target),
                    file_size=target.stat().st_size,
                )
            else:
                if client is None or target is None:
                    raise RuntimeError("Wanbang credentials are required for downloading.")
                direct_url = client.video_download_url(reference.gid)
                size = download_file(direct_url, target)
                result = DownloadResult(
                    reference.source_url,
                    reference.gid,
                    reference.video_url,
                    "downloaded",
                    keyword=reference.keyword,
                    path=str(target),
                    file_size=size,
                )
            results.append(result)
            print(f"  {result.status}")
            append_log(
                log_path,
                f"item_{result.status} index={index} gid={reference.gid} "
                f"path={result.path} size={result.file_size}",
            )
        except Exception as exc:
            result = DownloadResult(
                reference.source_url,
                reference.gid,
                reference.video_url,
                "failed",
                keyword=reference.keyword,
                error=str(exc),
            )
            results.append(result)
            print(f"  failed: {exc}")
            append_log(log_path, f"item_failed index={index} gid={reference.gid} error={exc}")
        finally:
            write_outputs(results, references, args.out_dir)
            append_log(
                log_path,
                f"summary_written count={len(results)} summary={(args.out_dir / 'summary.json').resolve()}",
            )
            if args.sleep > 0 and index < len(references):
                time.sleep(args.sleep)

    append_log(
        log_path,
        f"run_end downloaded={sum(1 for item in results if item.status == 'downloaded')} "
        f"reused={sum(1 for item in results if item.status == 'reused')} "
        f"failed={sum(1 for item in results if item.status == 'failed')}",
    )
    print((args.out_dir / "summary.json").resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
