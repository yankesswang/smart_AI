<!-- ppt-master-schema: spec-lock/v1 -->
# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## communication
- primary_language: zh-Hant-TW
- audience: 2026 中華電信智慧創新應用大賽評審，熟悉智慧製造題型，會追問資料真偽與對照基準
- objective: 讓評審接受「工安是硬限制不是加權項」的評判標準，相信八階段閉環的反制作弊架構，並認可外部公開資料集的驗證結果與已標出最敏感假設的 ROI
- core_message: 異常發生時 AI 不只警告，而是自動走完偵測到驗證恢復的八階段閉環，且每個可能作弊的環節都被架構擋掉
- consumption_mode: balanced

## mode
- mode: custom
- mode_references: pyramid, narrative
- mode_behavior: 以 pyramid 開局，第二頁即交出四組對照數字，每頁標題都是可被追問的判斷句；可信度章改用 narrative 的張力結構，每頁先講一個作弊風險再給出架構如何堵住，證據章回到先結論後數據。

## visual_style
- visual_style: custom
- visual_style_references: swiss-minimal, data-journalism
- visual_style_behavior: 在 presentation_core 版型的白底、圓角面板與髮絲線框架上，swiss-minimal 供給嚴格欄位網格與寬鬆頁邊；data-journalism 供給證據密度——每個數字下方帶來源分級行。色彩紀律極嚴：工業藍承載系統與結構，hazard red 只出現在工安與危害語意，驗證綠只出現在通過，其餘一律灰階；版型面板重上為 secondary_bg 與 divider。

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
- p15: images/fg_landing_5g.png | source=ai | crop=no-crop

## page_visualizations
- P10: table/metric_table
- P11: chart/horizontal_bar_chart
- P12: chart/horizontal_bar_chart
- P13: chart/grouped_bar_chart

## pptx_structure
- mode: structured
- template_reuse_scope: layout
- template_adherence: adaptive

## pptx_masters
- presentation_core_master: Presentation Core

## pptx_layouts
- fg_cover_split: presentation_core_master | FG Cover Split | P01
- fg_kpi_row: presentation_core_master | FG KPI Row | P02
- fg_three_card: presentation_core_master | FG Three Card | P03
- fg_full_canvas: presentation_core_master | FG Full Canvas | P04
- fg_two_picture: presentation_core_master | FG Two Pictures | P05
- fg_comparison: presentation_core_master | FG Comparison | P07
- fg_caption_focus: presentation_core_master | FG Caption Focus | P08
- fg_table_insight: presentation_core_master | FG Table and Insight | P10
- fg_chart_insight: presentation_core_master | FG Chart and Insight | P11
- fg_data_story: presentation_core_master | FG Data Story | P14
- fg_two_content: presentation_core_master | FG Two Content | P15

## page_pptx_layouts
- P01: fg_cover_split
- P02: fg_kpi_row
- P03: fg_three_card
- P04: fg_full_canvas
- P05: fg_two_picture
- P06: fg_three_card
- P07: fg_comparison
- P08: fg_caption_focus
- P09: fg_comparison
- P10: fg_table_insight
- P11: fg_chart_insight
- P12: fg_chart_insight
- P13: fg_chart_insight
- P14: fg_data_story
- P15: fg_two_content

## page_layouts
- P01: 11_editorial_split
- P02: 13_kpi_dashboard
- P03: 12_three_card
- P04: 02_title_content
- P05: 17_two_picture_caption
- P06: 12_three_card
- P07: 05_comparison
- P08: 08_content_caption
- P09: 05_comparison
- P10: 20_table_summary
- P11: 19_chart_insight
- P12: 19_chart_insight
- P13: 19_chart_insight
- P14: 15_data_story
- P15: 04_two_content

## forbidden
- `mask`, `<style>`, `class`, external CSS, `<foreignObject>`, `textPath`, `@font-face`, `<animate*>`, `<set>`, `<script>` / event attributes, `<iframe>`
- HTML named entities in text; write typography as raw Unicode and escape XML reserved characters
