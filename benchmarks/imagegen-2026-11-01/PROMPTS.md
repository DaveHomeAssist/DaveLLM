# DaveLLM ImageGen Harness Stress Test

Run each prompt through the visible DaveLLM **Generate** control on Walter. Use the
current local ImageGen configuration without prompt-specific manual tuning.

## Scoring

Each result is scored from 0-4 in five dimensions (20 points total):

1. Subject compliance
2. Composition and spatial compliance
3. Style fidelity
4. Detail and artifact control
5. Special constraint fidelity (text, count, lighting, or technical accuracy)

## Test prompts

### T01 — Photorealistic live event

> Photorealistic wide-angle documentary photo from front-of-house at a professional indoor concert: one video engineer at a switcher, a large LED wall showing abstract blue graphics, two projection screens, visible cable management, realistic stage haze and deep blue lighting, natural skin and hands, no logos, no text, 16:9 composition.

### T02 — Product photography

> Premium studio product photograph of a rugged black HDMI fiber converter on a matte charcoal pedestal, three-quarter view, one HDMI port and two status LEDs clearly visible, controlled rim light, realistic anodized metal and rubber textures, clean dark-gray background, no brand, no text, no extra connectors.

### T03 — Exact typography

> Minimal black event credential badge on a white tabletop. Print exactly three centered lines and no other text: "DIGITAL FOOTPRINTS" then "VIDEO ENGINEER" then "LOCAL 8". Crisp uppercase sans-serif typography, perfectly spelled, straight-on view, high contrast.

### T04 — Count and spatial reasoning

> Clean overhead photograph of exactly seven video adapters arranged in one straight horizontal row: three HDMI adapters on the left, one red USB-C adapter in the exact center, and three DisplayPort adapters on the right. Nothing overlaps. No other objects, no text, neutral gray background.

### T05 — Transparent-style icon

> Single centered broadcast camera icon, bold flat vector-like geometry, dark navy body with one cyan tally light, isolated object with generous empty margin, no shadow, no background scenery, no text, no watermark.

### T06 — Watercolor illustration

> Loose but controlled watercolor illustration of a rainy Philadelphia theater loading dock at dawn, two stagehands rolling one road case toward an open truck, wet pavement reflections, muted indigo and warm amber palette, visible paper grain, expressive brush edges, no text.

### T07 — Isometric technical scene

> Precise isometric 3D illustration of a compact video control room: one operator desk, exactly four monitors in a 2-by-2 grid, one rack with six evenly spaced devices, cable tray below the desk, clean architectural cutaway, soft neutral lighting, no people, no labels, no text.

### T08 — Low-light realism

> Realistic backstage photograph during a live show: a technician in black clothing checks a rack of video gear using a small flashlight, dim red work lights, bright stage spill through a curtain gap, believable high-ISO grain, preserved shadow detail, natural hands, no logos, no text.

### T09 — Food macro

> Extreme macro food photograph of a single cheesesteak cross-section, thin sliced beef, melted provolone, caramelized onions, crisp seeded roll, shallow depth of field, appetizing but natural texture, soft window light, no plate clutter, no text.

### T10 — Historical scene

> Historically grounded documentary-style photograph of a 1910 theater projection booth, two carbon-arc film projectors, one projectionist in period work clothes, metal film cans and fireproof shutters, warm practical light, realistic wear, absolutely no modern screens, LEDs, plastic cases, or contemporary clothing.

### T11 — Surreal concept art

> Cinematic surreal concept art of a giant translucent jellyfish floating above an empty city intersection at night, its tendrils made of glowing fiber-optic cables, wet asphalt reflecting magenta and cyan light, tiny lone pedestrian for scale, dramatic but coherent perspective, no text.

### T12 — Educational diagram

> Clean educational infographic showing a simple HDMI signal chain from left to right: laptop, HDMI cable, matrix switcher, fiber extender pair, LED processor, LED wall. Use six distinct objects connected by one continuous arrow path. White background, restrained blue and orange palette, labels must be short and readable, no decorative objects.

## Execution record

| ID | Seed | Duration | Score /20 | Output | Notes |
|---|---:|---:|---:|---|---|
| T01 | 4574776009973252000 | ~5-7s | 11 | T01.png | Strong concert look; wrong aspect ratio and incomplete spatial constraints. |
| T02 | 3248247712114751000 | ~5-7s | 10 | T02.png | Convincing material; two units, many connectors, hallucinated text. |
| T03 | 5927244741553246000 | ~5-7s | 4 | T03.png | Exact text and badge composition failed. |
| T04 | 8170998600718253000 | ~5-7s | 6 | T04.png | Adapter theme recognized; count, row, center, and no-text rules failed. |
| T05 | 6830882861670146000 | ~5-7s | 11 | T05.png | Usable flat-camera concept; background and margin constraints failed. |
| T06 | 6547974644942876000 | ~5-7s | 17 | T06.png | Best style result; credible watercolor loading-dock scene. |
| T07 | 8711942610562889000 | ~5-7s | 7 | T07.png | Isometric style worked; requested single room and exact counts failed. |
| T08 | 3339325313535493600 | ~5-7s | 12 | T08.png | Strong red backstage mood; rack/flashlight and lighting details missed. |
| T09 | 4718499695885711000 | ~5-7s | 17 | T09.png | Strong macro food result; slightly overprocessed texture. |
| T10 | 1100725506891116000 | ~5-7s | 14 | T10.png | Convincing period look; person and equipment counts drifted. |
| T11 | 5323677158216344000 | ~5-7s | 15 | T11.png | Strong concept and palette; extra people/cars and pseudo-text. |
| T12 | 6615511279579624000 | ~5-7s | 5 | T12.png | Diagram intent recognized; topology, object count, and labels failed. |
