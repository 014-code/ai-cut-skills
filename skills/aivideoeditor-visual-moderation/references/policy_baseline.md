# Visual Moderation Policy Baseline

Policy version: `visual-moderation-baseline-2026-07-29`

This baseline covers only:

- `military`: real-world concrete military-sensitive content, such as real military units, uniforms, weapons, insignia, flags, formations, vehicles, maps, operations, recruitment, propaganda, or dialogue naming specific forces, units, equipment, bases, operations, or military secrets.
- `political`: real-world concrete political-sensitive content, such as real political leaders, party/government organs, state emblems or flags, political slogans, protests, separatism, sovereignty or territorial dispute content, propaganda, or sensitive political events.
- `nsfw`: explicit nudity, sexual acts, exposed intimate body parts, pornographic visuals, lingerie or poses that are strongly sexualized, plus explicit sexual dialogue/subtitles.

The military and political categories are about concrete real-world sensitive content. Fictional, historical, game, anime, costume, or generic ancient city-defense scenes should not be treated as violations unless they expose real units, political figures, state symbols, operational details, or other specific sensitive identifiers.

## Actions

- `PASS`: no meaningful evidence for the scoped categories.
- `REVIEW`: category evidence exists but context is unclear or may be allowed with human judgment.
- `BLOCK`: high-confidence sensitive content, real-world political propaganda or severe political risk, explicit sexual content, or severe combinations.

## Baseline Rules

Use the strongest applicable action. `BLOCK` overrides `REVIEW`; `REVIEW` overrides `PASS`.

### Political Rules

- `BLOCK` for high-confidence real-world political propaganda, separatism, territorial/sovereignty dispute slogans, sensitive political-event content, or state-secret-like political content.
- `BLOCK` when subtitles/dialogue clearly contain prohibited political slogans, calls to political action, separatist claims, or sensitive political propaganda.
- `REVIEW` for real political leaders, party/government organs, national emblems/flags, government buildings, protests, official meetings, or political slogans when context is unclear.
- `PASS` for fictional governments, ancient court politics, generic words like "皇帝", "将军", "官府", "城主", or drama dialogue unless real-world political identifiers are present.

### NSFW Rules

- `BLOCK` for explicit nudity, sexual acts, exposed genitals, exposed female nipples, pornography, or strong model confidence above the blocking threshold.
- `BLOCK` for explicit sexual dialogue or subtitles that describe sexual acts, pornographic services, sexual solicitation, or sexual content involving minors.
- `REVIEW` for lingerie, underwear, sexualized posing, cleavage-heavy content, partial nudity, or ambiguous adult imagery.
- `REVIEW` for suggestive dialogue, euphemisms, or borderline erotic marketing language.
- Treat minor plus sexual evidence as severe and block immediately when such a signal exists.

### Military Rules

- `BLOCK` for military propaganda, recruitment, real-world operational military content, sensitive military identifier exposure, or military plus violence/weapon escalation.
- `BLOCK` for subtitles/dialogue containing real unit numbers, active operations, military locations, classified-like details, recruitment instructions, or propaganda slogans.
- `REVIEW` for real military uniforms, insignia, weapons, tanks, aircraft, camouflage, formations, or armed personnel when context is unclear.
- `PASS` may be acceptable for clearly fictional toy/game/anime content only when both detector and VLM evidence support that context.
- `PASS` for ancient/fantasy/historical costume scenes, generic city walls, martial arts weapons, fictional military-like formations, or drama dialogue unless real-world identifiers are present.

## Dialogue and Subtitle Rules

Treat OCR, burned-in subtitles, ASR transcript, title text, and overlay text as first-class evidence.

- Normalize text into timestamped segments when possible.
- Match risky text to the same `military`, `political`, and `nsfw` categories.
- Keep visual and text evidence separate in logs.

Do not over-trigger on generic words. For example, "女囚", "城墙", "开荒", "将军" in a fictional historical drama are not real-world military violations by themselves.

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
- policy version

Never store secrets in skill fixtures. Mask sensitive OCR examples when fixtures are committed or shared.
