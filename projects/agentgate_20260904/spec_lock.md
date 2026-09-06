<!-- ppt-master-schema: spec-lock/v1 -->
# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## communication
- primary_language: zh-Hant-TW
- audience: 2026 中華電信智慧創新應用大賽評審，兼具技術判讀與商業評估，未看過本系統且會追問數字來源
- objective: 讓評審相信六道 Gate 是可執行可量測可消融的工程系統，並能複述來源信任分級、後果預演與不可否認稽核鏈三個差異點
- core_message: AgentGate 是夾在 AI Agent 與真實系統之間的治理層，讓企業第一次敢把動手的權限交給 Agent
- consumption_mode: balanced

## mode
- mode: custom
- mode_references: pyramid, briefing
- mode_behavior: 以 pyramid 開局，第二頁即交出裁決結論與三個關鍵數字，其後每章先給判斷句再以可重跑證據支撐；證據章改採 briefing 的中性完整語氣，讓對照表、消融與誠實邊界等重量並陳。

## visual_style
- visual_style: custom
- visual_style_references: dark-tech, swiss-minimal
- visual_style_behavior: dark-tech 供給暗場基底、發光強調與幾何精確的關卡管線語彙，被否決路徑轉負向色並保留在畫面上；swiss-minimal 供給嚴格欄位網格、大量留白與髮絲線分隔。容器一律直角，強調只靠色相與亮度階，數字與規則編號以等寬字體單獨成階。

## colors
- background: #0B111C
- secondary_bg: #141D2E
- primary: #4C9AFF
- accent: #FFB020
- secondary_accent: #2FD3A6
- body_text: #E4ECF7
- secondary_text: #94A5BC
- divider: #26324A
- surface: #182337
- grid: #1E2B42
- positive: #2FD3A6
- warning: #FFB020
- negative: #FF6B6B

## typography
- font_family: Microsoft JhengHei, Segoe UI, sans-serif
- title_family: Microsoft JhengHei, Arial Black, sans-serif
- body_family: Microsoft JhengHei, Segoe UI, sans-serif
- display_family: Microsoft JhengHei, Arial Black, sans-serif
- data_family: Consolas, monospace
- body: 24
- title: 42
- subtitle: 32
- lead: 30
- display: 96
- annotation: 18
- footnote: 16
- data: 22

## icons
- library: tabler-outline
- stroke_width: 2
- inventory: tabler-outline/shield-lock, tabler-outline/alert-triangle, tabler-outline/file-search, tabler-outline/route, tabler-outline/scale, tabler-outline/player-play, tabler-outline/user-check, tabler-outline/link, tabler-outline/lock, tabler-outline/database, tabler-outline/robot, tabler-outline/chart-bar, tabler-outline/clock, tabler-outline/currency-dollar, tabler-outline/fingerprint, tabler-outline/ban, tabler-outline/eye, tabler-outline/device-mobile

## page_rhythm
- P01: anchor
- P02: anchor
- P03: dense
- P04: breathing
- P05: anchor
- P06: dense
- P07: dense
- P08: breathing
- P09: dense
- P10: dense
- P11: breathing
- P12: dense
- P13: dense
- P14: breathing
- P15: anchor

## images
- p01: images/ag_cover_hero.png | source=ai | crop=no-crop
- p04: images/agentgate_live_agent_crop.png | source=user | crop=no-crop
- p05: images/ag_gates_bg.png | source=ai | crop=adaptive
- p09: images/agentgate_evidence_crop.png | source=user | crop=no-crop
- p11: images/agentgate_console_crop.png | source=user | crop=no-crop

## page_visualizations
- P12: table/metric_table
- P13: chart/column_chart

## pptx_structure
- mode: flat

## forbidden
- `mask`, `<style>`, `class`, external CSS, `<foreignObject>`, `textPath`, `@font-face`, `<animate*>`, `<set>`, `<script>` / event attributes, `<iframe>`
- HTML named entities in text; write typography as raw Unicode and escape XML reserved characters
