<!-- ppt-master-schema: spec-lock/v1 -->
# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## communication
- primary_language: zh-Hant-TW
- audience: 2026 中華電信智慧創新應用大賽評審，熟悉智慧製造題型，會追問資料真偽與對照基準
- objective: 讓評審接受「工安是硬限制不是加權項」的評判標準，相信八階段閉環的反制作弊架構，並認可兩份外部公開資料集的驗證結果與已標出最敏感假設的 ROI
- core_message: 異常發生時 AI 不只警告，而是自動走完偵測到驗證恢復的八階段閉環，且每個可能作弊的環節都被架構擋掉
- consumption_mode: balanced

## mode
- mode: custom
- mode_references: pyramid, narrative
- mode_behavior: 以 pyramid 開局，第二頁即交出三組對照數字，每頁標題都是可被追問的判斷句；可信度章改用 narrative 的張力結構，每頁先講一個作弊風險再給出架構如何堵住，證據章回到先結論後數據。

## visual_style
- visual_style: custom
- visual_style_references: swiss-minimal, data-journalism
- visual_style_behavior: swiss-minimal 供給嚴格欄位網格、寬鬆頁邊與髮絲線分隔，容器一律直角、無陰影無漸層；data-journalism 供給證據密度——多欄微型圖表、側欄註解、每個數字下方帶來源分級行。色彩紀律極嚴：工業藍承載系統與結構，hazard red 只出現在工安與危害語意，驗證綠只出現在通過，其餘一律灰階。

## colors
- background: #FFFFFF
- secondary_bg: #F1F4F8
- primary: #0F4C81
- accent: #E2231A
- secondary_accent: #1B7F5A
- body_text: #16202B
- secondary_text: #5A6B7D
- divider: #D5DDE5
- surface: #FAFBFC
- grid: #E8EDF2
- positive: #1B7F5A
- warning: #E8890C
- negative: #E2231A

## typography
- font_family: Microsoft JhengHei, Segoe UI, sans-serif
- title_family: Microsoft JhengHei, Segoe UI, sans-serif
- body_family: Microsoft JhengHei, Segoe UI, sans-serif
- data_family: Consolas, monospace
- body: 24
- title: 42
- subtitle: 32
- lead: 30
- display: 88
- annotation: 18
- footnote: 16
- data: 22

## icons
- library: chunk-filled
- inventory: chunk-filled/factory, chunk-filled/triangle-exclamation, chunk-filled/gauge-high, chunk-filled/temperature-high, chunk-filled/waveform, chunk-filled/magnifying-glass, chunk-filled/route, chunk-filled/sliders, chunk-filled/shield-check, chunk-filled/traffic-cone, chunk-filled/badge-check, chunk-filled/person-walking, chunk-filled/play, chunk-filled/circle-checkmark, chunk-filled/clipboard, chunk-filled/wrench, chunk-filled/robot, chunk-filled/chart-bar, chunk-filled/coin, chunk-filled/stopwatch, chunk-filled/video-camera

## page_rhythm
- P01: anchor
- P02: anchor
- P03: dense
- P04: anchor
- P05: breathing
- P06: dense
- P07: dense
- P08: dense
- P09: breathing
- P10: dense
- P11: breathing
- P12: dense
- P13: dense
- P14: dense
- P15: anchor

## images
- p01: images/fg_cover_hero.png | source=ai | crop=no-crop
- p05a: images/guardian_hardware_state.png | source=user | crop=no-crop
- p05b: images/guardian_agent_loop.png | source=user | crop=no-crop
- p05c: images/guardian_cam01.png | source=user | crop=no-crop
- p05d: images/guardian_plan_matrix.png | source=user | crop=no-crop
- p05e: images/guardian_verify.png | source=user | crop=no-crop
- p11: images/fg_hazard_zone.png | source=ai | crop=no-crop
- p15: images/fg_landing_5g.png | source=ai | crop=no-crop

## page_visualizations
- P10: table/metric_table
- P11: chart/horizontal_bar_chart
- P12: chart/column_chart
- P13: chart/grouped_bar_chart
- P14: chart/pareto_chart

## pptx_structure
- mode: flat

## forbidden
- `mask`, `<style>`, `class`, external CSS, `<foreignObject>`, `textPath`, `@font-face`, `<animate*>`, `<set>`, `<script>` / event attributes, `<iframe>`
- HTML named entities in text; write typography as raw Unicode and escape XML reserved characters
