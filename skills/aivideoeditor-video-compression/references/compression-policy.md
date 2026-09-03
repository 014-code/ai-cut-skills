# Compression policy

## Decision order

1. Probe once and cache the metadata by source fingerprint.
   The report includes the measured/estimated video-stream bitrate.
2. If a configured minimum video bitrate is not exceeded, copy the source and
   record `skip_low_bitrate`; do not introduce another lossy generation.
3. If the selected target codec and size constraints already match, copy the
   source without re-encoding.
4. Use CRF-based encoding for normal quality-first compression.
   If the encoded candidate is not smaller than the source, keep the original
   bytes and record `keep_original_no_savings`.
5. In strict-size mode, retry from the original source and only then reduce
   audio bitrate, long-edge resolution, and FPS.
6. Validate the output and keep the original beside the derived artifact.

## Quality tradeoffs

- x264 CRF 22 is the default H.264 starting point.
- Raising CRF by 1-2 reduces size but increases visible loss.
- Resolution and FPS changes should be later fallbacks, not defaults.
- H.265 and AV1 can reduce storage size, but must remain opt-in because the
  consuming platform may not support them.
- For already-low-bitrate H.264, raising H.264 CRF may barely reduce size;
  HEVC CRF 28 is a useful opt-in storage profile. Tag MP4 HEVC as `hvc1`.
- ZIP compression is not a useful video compression method for MP4/MOV.
