# Visual Moderation Policy Baseline

Policy version: `visual-moderation-baseline-2026-07-28`

This baseline covers only:

- `military`: real-world concrete military-sensitive content, such as real military units, uniforms, weapons, insignia, flags, formations, vehicles, maps, operations, recruitment, propaganda, or dialogue naming specific forces, units, equipment, bases, operations, or military secrets.
- `id_document`: real ID cards, passports, driver licenses, permits, certificates, government credentials, visible credential numbers, or OCR/dialogue text that exposes identity data.
- `nsfw`: explicit nudity, sexual acts, exposed intimate body parts, pornographic visuals, lingerie or poses that are strongly sexualized, plus explicit sexual dialogue/subtitles.

The military and ID categories are about concrete real-world sensitive content. Fictional, historical, game, anime, costume, or generic ancient city-defense scenes should not be treated as violations unless they expose real units, symbols, credentials, operational details, or other specific sensitive identifiers.

## Actions

- `PASS`: no meaningful evidence for the scoped categories.
- `REVIEW`: category evidence exists but context is unclear or may be allowed with human judgment.
- `BLOCK`: high-confidence sensitive content, exposed identity credentials, explicit sexual content, or severe combinations.

## Baseline Rules

Use the strongest applicable action. `BLOCK` overrides `REVIEW`; `REVIEW` overrides `PASS`.

### ID / Credential Rules

- `BLOCK` when an identity document is visible and OCR or detector evidence is high confidence.
- `BLOCK` when OCR includes identity labels plus plausible credential numbers.
- `BLOCK` when subtitles/dialogue expose a real person's ID number, passport number, driver license number, phone plus identity linkage, address plus credential linkage, or credential verification details.
- `REVIEW` when a card, certificate, passport-like page, or official credential is visible but OCR is weak.
- `PASS` for fictional props, generic words like "证件" without private details, or UI labels that do not expose real credentials.

### NSFW Rules

- `BLOCK` for explicit nudity, sexual acts, exposed genitals, exposed female nipples, pornography, or strong model confidence above the blocking threshold.
- `BLOCK` for explicit sexual dialogue or subtitles that describe sexual acts, pornographic services, sexual solicitation, or sexual content involving minors.
- `REVIEW` for lingerie, underwear, sexualized posing, cleavage-heavy content, partial nudity, or ambiguous adult imagery.
- `REVIEW` for suggestive dialogue, euphemisms, or borderline erotic marketing language.
- Do not treat faces as NSFW redaction targets by default; face masking requires a separate privacy/identity policy, not the sexual-content policy.
- Do not mask ordinary covered chest or shoulder/neck skin. Covered-breast or upper-chest candidates should be redacted only when cleavage is visually obvious, such as a clear central cleavage gap or heavy sexualized exposure.
- Treat minor plus sexual evidence as severe and block immediately when such a signal exists.

### Military Rules

- `BLOCK` for military propaganda, recruitment, real-world operational military content, official military credential exposure, or military plus violence/weapon escalation.
- `BLOCK` for subtitles/dialogue containing real unit numbers, active operations, military locations, classified-like details, recruitment instructions, or propaganda slogans.
- `REVIEW` for real military uniforms, insignia, weapons, tanks, aircraft, camouflage, formations, or armed personnel when context is unclear.
- `PASS` may be acceptable for clearly fictional toy/game/anime content only when both detector and VLM evidence support that context.
- `PASS` for ancient/fantasy/historical costume scenes, generic city walls, martial arts weapons, fictional military-like formations, or drama dialogue unless real-world identifiers are present.

## Dialogue and Subtitle Rules

Treat OCR, burned-in subtitles, ASR transcript, title text, and overlay text as first-class evidence.

- Normalize text into timestamped segments when possible.
- Match risky text to the same `military`, `id_document`, and `nsfw` categories.
- Keep visual and text evidence separate in logs so redaction can target the right layer.
- For hard hits, emit redaction targets for the visual region, subtitle region, audio segment, or text overlay.

Do not over-trigger on generic words. For example, "女囚", "城墙", "开荒", "将军" in a fictional historical drama are not real-world military violations by themselves.

## Redaction Rules

When action is `REVIEW` or `BLOCK`, emit a `redactions` array:

- `visual_mosaic`: blur or pixelate a sensitive visual region such as a real ID card, credential number, explicit body area, insignia, weapon detail, or military map.
- `text_mosaic`: cover a subtitle/OCR text region that contains risky words or private data.
- `audio_mute`: mute or replace dialogue audio for the timestamp span containing prohibited speech.
- `subtitle_replace`: replace unsafe subtitle text with neutral masked text such as `[已处理]`.

Prefer targeted masking over full-frame masking. Use full-frame masking only when no reliable region is available and the content is a confirmed `BLOCK`.

## Suggested Thresholds

- High confidence: `>= 0.85`
- Medium confidence: `>= 0.55`
- Low confidence: `< 0.55`

Thresholds are not the full policy. Keyword evidence, OCR evidence, and sensitive combinations can override pure score thresholds.

## Evidence Requirements

Each decision must keep:

- normalized scores for all three categories
- labels or OCR snippets used as evidence
- subtitle/dialogue snippets used as evidence
- VLM rationale when a VLM is used
- final rule reason
- redaction targets for non-pass decisions
- policy version

Never store secrets or raw private credentials in skill fixtures. Mask sensitive OCR examples when fixtures are committed or shared.
