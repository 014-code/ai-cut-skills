# Douyin Reference Contract

Use this contract when another Skill needs normalized Douyin inputs without owning short-link, GID, Wanbang, or download logic.

## Source Of Truth

`scripts/douyin_reference_core.py` exclusively owns:

- short-link redirect resolution;
- GID extraction and canonical video URLs;
- Wanbang `item_search_video` and `item_get_video`;
- MP4 validation and atomic `.part` download replacement;
- reference Manifest serialization.

Do not copy these functions into a consuming Skill. Locate the installed module, load it, and call `resolve_references`, `WanbangClient`, `download_file`, or `load_reference_manifest`.

## Manifest

`references.json` has this shape:

```json
{
  "schema_version": 1,
  "generator": "douyin-video-toolkit",
  "items": [
    {
      "source_url": "https://v.douyin.com/example/",
      "gid": "7380000000000000001",
      "video_url": "https://www.douyin.com/video/7380000000000000001",
      "keyword": "",
      "status": "resolved",
      "error": ""
    }
  ]
}
```

Fields:

- `source_url`: original input or search result source.
- `gid`: normalized Douyin GID; empty when resolution failed.
- `video_url`: canonical public page URL; empty when resolution failed.
- `keyword`: originating Wanbang search keyword when applicable.
- `status`: `resolved` or `failed` at reference-building time.
- `error`: failure reason; empty after successful resolution.

Consumers may add their own business-query or download state in separate output files. Do not write Mogong status, brand metadata, or other business fields back into this Manifest.
