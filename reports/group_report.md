# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Mọi con số lấy từ file do script sinh ra (`reports/selfcheck_dev_*.json`, `reports/table1_dev_selfcheck.md`,
> `out/*/run_info.json`). Số chính thức trên split eval do giảng viên chấm bằng `gt/` (repo học viên không có).
> Tài liệu học tập, không phải tư vấn pháp lý.

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | G04 |
| Challenge | T09 · Multi-Market Privacy Decision Gate |
| Thành viên + vai trò | 1. Trần Tuấn Anh (26ai.anhtt5@vinuni.edu.vn) — làm một mình, đảm nhận cả 5 vai trò (Lead/Integrator, Data & fixtures, Core algorithm, QA/metric, Review + report) |
| Git URL | https://github.com/plastma65/K5-L2-DAY06-TranTuanAnh-2A202603230-DataGovernanceAndPrivacy |
| Tag `demo-180m` (commit) | `c0fd9d7` (code + report trước human review; review evidence thêm ở commit sau) |
| Tag `final-24h` (commit) | commit nộp cuối, xem `git show final-24h` |
| Deadline (giờ VN) | 12:00 ngày 15/11/2026 |

## 01 · Problem statement

- **Ai chịu hậu quả khi hệ thống sai?** Người đi đường, tài xế, hành khách có hình ảnh trong dữ liệu ADAS (nếu gate cho GO sai);
  công ty (phạt theo Luật 91/2025, GDPR; dừng dự án); đội legal/DPO (nếu gate HOLD quá nhiều → nghẽn review).
- **Điểm nghẽn:** mỗi case thu thập dữ liệu đa thị trường (VN / EU / US-CA / SG) phải được rà theo luật khác nhau; checklist
  "có consent là xong" bỏ sót chuyển dữ liệu xuyên biên giới, DPIA, tính hợp lệ của consent, retention, notice.
- **Đầu vào → đầu ra:** 12 file `cases/CASE-XX.json` + policy YAML có version → `decisions.csv` (GO/HOLD/REWORK + reason codes),
  `audit_checklist.csv` (case × rule × nguồn luật), queue cho người duyệt legal, `audit_log.jsonl`, `decision.json`.
- **Tiêu chí (chốt trước eval):** precedence HOLD > REWORK > GO. HOLD = vi phạm nghiêm trọng / chưa thể xác định (không có căn cứ
  pháp lý, chuyển xuyên biên giới thiếu cơ chế/hồ sơ, thiếu DPIA, trẻ em, ambiguity, thiếu bằng chứng, market ngoài phạm vi).
  REWORK = lỗi sửa được (consent gộp, retention vô hạn/quá dài, thiếu mã hoá, raw PII trên cloud, thiếu notice).
  GO = không rule nào fire. Mọi case phải được người phụ trách legal phê duyệt cuối.
- **Giả định cần DPO/legal xác nhận:** số điều luật trong `source.ref` (rule `needs_legal_review: true`); ngưỡng retention 365 ngày
  (internal policy); allowlist cơ chế chuyển giao; ADAS thuộc Phụ lục I AI Act; schema field của case phản ánh đúng thực tế.

## 02 · Data & provenance

| Split | N | Nguồn | License | Synthetic? |
|---|---|---|---|---|
| dev | 8 (CASE-01..08) | `fixtures/t09_privacy_gate/` (giảng viên) | CC0-1.0 | có |
| eval | 4 (CASE-09..12) | như trên; gold chỉ ở `gt/` của giảng viên | CC0-1.0 | có |

- Schema: `case_schema` trong `policies/t09_policy_v1.yaml` (27 path có kiểu, required/nullable).
- Seed 42; engine deterministic (không dùng random). `as_of` cố định 2026-11-14 để xử lý `effective_date` tái lập được.
- `manifest.csv` verified (sha256): **có** — `python -m tools.verify_manifests` → 14 manifests, 0 failing (sau khi sửa CRLF, xem 06).
- Gold tự gán cho dev: `reports/dev_labels_t09.csv` (8 case, gán theo văn bản luật, có cột rationale). Không có dữ liệu thật.

## 03 · Research & design

- **Tham khảo** (xem 11): Luật 91/2025/QH15 (đối chiếu toàn văn số điều Đ.9, 10, 19, 20, 21, 22, 24), GDPR, CCPA/CPRA, PDPA 2012,
  AI Act + 2026/1744 (mốc theo `docs/tracks/T09.md`). Chọn **rule engine có version + nguồn** thay vì model ML/LLM vì: N=12,
  quyết định pháp lý cần giải thích được (mỗi kết luận → rule id → điều luật), phải chạy offline, tái lập byte-for-byte.
- **Data flow:**
  ```
  cases/*.json ──► load (sha256 manifest, policy YAML + sha256) ──► check_schema (fail-safe)
     ──► rule DSL {path, op, value} theo market ──► HOLD > REWORK > GO + reason codes + FLAG
     ──► rank (B = 12, HOLD/ambiguity/legal-review trước) ──► contact_sheet.html (Export CSV)
     ──► apply_review (approve/edit/reject/escalate, note + reviewer bắt buộc) ──► audit_log.jsonl
     ──► decide (run-level: HOLD PENDING_LEGAL_APPROVAL cho tới khi mọi case được ký)
  ```
- **Thành phần offline:** không có model/weights. Chỉ Python 3.11.9 + pandas 3.1.1 + PyYAML 6.0.3.
- **Trade-offs:**
  - DSL tự viết thay vì `eval()` chuỗi điều kiện như file mẫu → không thực thi code từ rule file, validate được lúc load.
  - Fail-safe (thiếu/sai kiểu/không rõ → HOLD) đổi lấy false HOLD cao hơn khi dữ liệu bẩn; chấp nhận vì false GO đắt hơn nhiều.
  - Rule `FLAG` (dữ liệu nhạy cảm) không đổi quyết định để tránh tăng false HOLD, nhưng bắt buộc legal xem.
  - Không có rule LIA cho EU legitimate interest (chưa có field trong schema) → ghi ở 09.

## 04 · Baseline vs PoC

- **Baseline 1 (bắt buộc, sai có chủ ý):** "có consent thì GO, ngược lại HOLD" — `src/baselines/t09_consent_only.py`.
- **Baseline 2 (tầm thường):** HOLD tất cả — `src/baselines/t09_all_hold.py`.
- **PoC:** `src/pipeline.py` + `src/t09/policy_engine.py` + policy `g-t09-1.1` (sha256 ghi trong `out/poc*/run_info.json`).
- Cùng 12 case / cùng seed 42 / cùng output contract / cùng evaluator (`src.evaluate --track t09`).

**Bảng 1 · Tự kiểm trên dev (N=8, gold tự gán)** — `python -m src.compare reports/selfcheck_dev_baseline.json reports/selfcheck_dev_poc.json`
(`reports/table1_dev_selfcheck.md`, git bf543fe):

| Metric (unit) | Baseline consent-only | Baseline all-HOLD | PoC g-t09-1.1 | Ghi chú |
|---|---|---|---|---|
| **Primary: Critical-miss Rate** (lower) | 0.4286 (3/7) · Wilson 95% [0.158, 0.750] | 0.0000 (0/7) | **0.0000 (0/7)** · Wilson 95% [0.000, 0.354] | N risky = 7 |
| Guardrail False HOLD ≤ 0.5 | FAIL (1.0, 1/1) | FAIL (1.0, 1/1) | **PASS (0.0, 0/1)** | N GO = 1 → guardrail gần như không có sức thống kê |
| High-risk HOLD Recall | 0.5714 | 1.0000 | 1.0000 | |
| Exact decision accuracy | 0.375 (3/8) | 0.625 (5/8) | 1.000 (8/8) · Wilson 95% [0.676, 1.000] | |
| Reason-code match (risky) | 0.2857 | 0.1429 | 1.0000 | |
| Runtime / 100 items (s) | 0.48 | — | 0.91 | Windows 11, Intel Family 6 Model 213, Python 3.11.9 |

**Đọc trung thực:** PoC 8/8 trên dev là **kỳ vọng, không phải thành tích** — rule được viết khi đã đọc dev và gold dev do chính
nhóm gán. Baseline all-HOLD cũng đạt critical-miss 0 nhưng FAIL guardrail → primary của nó không hợp lệ.

**Sau human review** (`reports/selfcheck_dev_poc_reviewed.json`, cùng gold dev): Critical-miss 0.0 (0/7), false HOLD 0.0 (0/1,
guardrail PASS), exact accuracy **0.875 (7/8)**. Case lệch duy nhất là CASE-03: người duyệt nâng REWORK → HOLD (xem 07), khác
nhãn tự gán REWORK. Nhóm báo cả hai số (engine trước review và quyết định cuối sau review), không chỉnh gold dev cho khớp.

**Eval (CASE-09..12) — quyết định của nhóm, chưa có gold:**

| Case | Market | Baseline consent-only | PoC g-t09-1.0 | PoC g-t09-1.1 (engine) | Sau human review | Reason codes (1.1) |
|---|---|---|---|---|---|---|
| CASE-09 | IN | GO | HOLD | HOLD | HOLD (approve) | AMBIGUOUS_NEEDS_LEGAL; MINORS; MARKET_NOT_COVERED |
| CASE-10 | EU | HOLD | GO | GO | GO (approve) | — (ghi chú UPCOMING: R-EU-AIA-01 từ 2028-08-02) |
| CASE-11 | US-CA | HOLD | HOLD | HOLD | HOLD (escalate) | AMBIGUOUS_NEEDS_LEGAL |
| CASE-12 | VN | HOLD | HOLD | HOLD | HOLD (approve) | NO_LAWFUL_BASIS; RETENTION_EXCESSIVE; NO_ENCRYPTION |

Human review không đổi quyết định nào trên eval. `decisions.csv` cuối có cả `proposed_decision` (engine) và `decision` (sau review).
Số chính thức: `make eval-baseline eval-poc compare TRACK=t09` khi có overlay `gt/` của giảng viên.

## 05 · Evaluation protocol

- Primary `Critical-miss Rate` = case gold ≠ GO mà dự đoán GO / case gold ≠ GO; chỉ hợp lệ khi guardrail false HOLD ≤ 0.5.
  Đơn vị = case; ghép theo `case_id`; thiếu case = GO (worst case). Chi tiết: `reports/metric_contract.md`.
- Không có threshold học từ dữ liệu; policy `g-t09-1.0` chốt trước khi chạy eval. `g-t09-1.1` chỉ sửa lỗi red-team tìm bằng
  case tổng hợp (không dựa trên eval) và **không đổi quyết định nào** trên 12 case fixture (so sánh 1.0 vs 1.1 ở bảng trên).
- Minh bạch: khi chạy thử pipeline, nhóm đã **thấy output dự đoán** của CASE-09..12 (không thấy gold). Không rule nào được
  thêm/sửa dựa trên các output này; mọi thay đổi 1.0 → 1.1 truy được tới finding RT-xx trong `reports/redteam_t09.md`.
  Ở bước human review (sau khi policy 1.1 đã chốt), người duyệt đọc đầy đủ CASE-09..12 — đó là vai trò "người phụ trách phê duyệt
  cuối" theo đề. Các hạn chế của engine phát hiện trong lúc review eval được **ghi vào 09, không sửa vào policy** trước khi chấm.
- Độ bất định: N nhỏ → báo từng case + Wilson 95% CI; không ngoại suy.
- Lệnh: `python -m src.evaluate --track t09 --pred out/poc/decisions.csv --out reports/metrics_poc.json` (cần `gt/`);
  tự kiểm: `python -m src.evaluate --track t09 --gt reports/dev_labels_t09.csv --pred out/poc/decisions.csv --split dev`.

## 06 · Result slices

| Slice (dev) | N | Baseline consent-only đúng | PoC đúng | Ghi chú |
|---|---|---|---|---|
| gold HOLD | 5 | 3/5 | 5/5 | baseline cho GO ở CASE-02, CASE-07 |
| gold REWORK | 2 | 0/2 | 2/2 | baseline: CASE-03 GO, CASE-05 HOLD |
| gold GO | 1 | 0/1 | 1/1 | baseline HOLD CASE-06 (SG legitimate interest hợp lệ) |
| market VN | 3 | 1/3 | 3/3 | |
| market EU | 3 | 2/3 | 3/3 | |
| market US-CA / SG | 1 / 1 | 0/1 / 0/1 | 1/1 / 1/1 | |

- **Failure case 1 — kết luận hợp pháp chỉ dựa vào consent** (failure mode bắt buộc của đề). CASE-07 (VN): consent hợp lệ
  (granular, withdrawable, có record) nhưng `transfer.cross_border=true`, `destination=SG`, `transfer_impact_assessment=false`.
  Baseline → **GO** (critical miss). Root cause: checklist không xét Luật 91 Đ.20 (hồ sơ đánh giá tác động chuyển dữ liệu
  xuyên biên giới). PoC → HOLD `XBORDER_NO_IMPACT_ASSESSMENT` (R-VN-02). Tương tự CASE-02 (EU, nước thứ ba không đủ mức bảo vệ,
  `mechanism=null`) → PoC HOLD `XBORDER_NO_MECHANISM` (GDPR Ch. V). Test: `tests/test_t09_policy.py::test_valid_consent_does_not_cure_vn_cross_border`.
- **Failure case 2 — engine fail-open với dữ liệu bẩn** (red-team, xem 07). Ví dụ RT-03: `transfer.cross_border: "true"` (string)
  bị op `is_true` đọc thành "không chuyển" → GO. Root cause: engine chỉ fail-safe ở `not_true`, còn `is_true`, blocklist `in [...]`
  và so sánh số thì fail-open; kiểm tra required field chỉ xét key top-level. Sửa: `case_schema` có kiểu → HOLD `EVIDENCE_MISSING`.
- **Failure mode reproducibility — CRLF trên Windows:** Git for Windows (`core.autocrlf=true`) đổi LF→CRLF khi clone → sha256
  của 14 file text lệch manifest → `load()` dừng (exit 2). Sửa: `.gitattributes` (`fixtures/** -text`) + checkout lại.
- **Latency:** 12 case trong ~0.1 s (0.91 s / 100 case), CPU, không GPU.

## 07 · Human & decision

- **Review queue:** `out/poc/review_queue.csv` + `contact_sheet.html`, B = 12 (mọi case cần legal ký). Ưu tiên
  `100·severity + 20·ambiguity + 10·needs_legal_review + 3·số rule fired + số rule upcoming` (`src/pipeline.py::review_priority`).
- **Override + audit:** reviewer chọn approve / edit (override GO|HOLD|REWORK + **note bắt buộc**) / reject / escalate (→ HOLD;
  mã `ESCALATED_TO_DPO` chỉ thêm khi quyết định thay đổi). Bắt buộc tên reviewer; một case chỉ một dòng quyết định; override chỉ
  nhận GO/HOLD/REWORK. Mỗi hành động ghi vào `audit_log.jsonl` (actor, before, after, reason).

**Human review thật (2026-11-14, reviewer: Trần Tuấn Anh, Data Owner G04)** — bằng chứng trong `reports/review_evidence/`
(`review_queue.csv` → `review_queue_reviewed.csv` export từ `contact_sheet.html`, `decisions_final.csv`, `audit_log.jsonl`
với 12 sự kiện `legal_review`, `decision.json` = **GO `ALL_CASES_SIGNED_OFF`**). Mỗi case được đối chiếu: dữ kiện gốc (JSON) →
rule FIRED/PASSED trong `audit_checklist.csv` → điều luật → mức độ.

| Case | Engine | Reviewer | Cuối | Nhận định chính của người duyệt |
|---|---|---|---|---|
| CASE-01 | HOLD | approve | HOLD | VN không có legitimate interest (Luật 91 Đ.9/Đ.19), không consent |
| CASE-02 | HOLD | approve | HOLD | Consent hợp lệ không thay cơ chế chuyển giao Ch. V (nước thứ ba không adequacy, mechanism null) |
| CASE-03 | REWORK | **edit → HOLD** | **HOLD** | Consent gộp/không rút được = không có consent hợp lệ → thực chất không có căn cứ; kèm ảnh mặt raw, chưa ẩn danh, lưu vô hạn trên cloud → phải dừng xử lý |
| CASE-04 | HOLD | approve | HOLD | Thiếu DPIA (Art. 35(3)(c)), thêm camera cabin; cờ dữ liệu nhạy cảm; LIA chưa được engine kiểm |
| CASE-05 | REWORK | approve | REWORK | CCPA không có lawful basis; chỉ thiếu notice at collection → ngừng thu mới tới khi có notice |
| CASE-06 | GO | approve | GO | PDPA legitimate interests có đánh giá (R-SG-02 đạt), có notice, các biện pháp khác đạt |
| CASE-07 | HOLD | approve | HOLD | Consent hợp lệ nhưng chuyển sang SG chưa có hồ sơ ĐGTĐ (Luật 91 Đ.20) |
| CASE-08 | HOLD | approve | HOLD | Căn cứ unknown + mơ hồ processor/joint controller (Art. 26/28) là thật |
| CASE-09 | HOLD | approve | HOLD | Ghi chú thực chất là luật DPDP sắp hiệu lực, **không phải ambiguity**; giữ HOLD vì minors_possible = true và IN chưa có policy |
| CASE-10 | GO | approve | GO | Soát đủ 12 field; LI có LIA (`lia_done = true`, **engine không đọc field này**, kiểm thủ công); chuyển tới nước adequacy |
| CASE-11 | HOLD | **escalate** | HOLD | Chưa rõ công ty có đạt ngưỡng "business" của CCPA 2026 → chuyển legal/finance xác định |
| CASE-12 | HOLD | approve | HOLD | Legitimate interest ở VN; retention 3650 ngày; raw trên cloud không mã hoá; **mâu thuẫn** blur-before-upload vs raw_retained |
- **Decision policy:** case-level theo rule (reason code + rule id + nguồn trong `audit_checklist.csv`); run-level `decision.json`
  với owner "Trần Tuấn Anh (Data Owner, G04)": HOLD `PENDING_LEGAL_APPROVAL` cho tới khi 12/12 case có quyết định của người duyệt.
- **Không tự động release khi:** có ambiguity, trẻ em, thiếu/sai kiểu bằng chứng, market chưa có rule, hoặc chưa có chữ ký legal.

**Red-team độc lập (khai báo dùng AI):** nhóm dùng **Fable 5.2** (model AI, chạy trong một session riêng) để red-team policy engine,
với ràng buộc: không đọc CASE-09..12, không sửa code, chỉ tạo `tests/test_t09_redteam.py` + `reports/redteam_t09.md`.
Kết quả 20 finding (5 critical false GO, 12 major, 3 minor), mỗi finding là một test `xfail(strict=True)`; nhóm kiểm lại từng
test, sửa trong policy `g-t09-1.1` và gỡ marker trong các commit fix (cặp "test fail → fix": `dec045f` → `74d633e`, `f6ad721`).
Phần rà trích dẫn của Fable làm từ trí nhớ; nhóm đã đối chiếu toàn văn Luật 91 và phát hiện Đ.20/Đ.21 bị hoán đổi trong policy
(và trong `rules/policy_example.yaml` của fixture).

## 08 · Reproduce & tests

```bash
make setup          # Windows: python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt
python -m src.main --input fixtures/t09_privacy_gate --config configs/lab.yaml --output out/poc --offline --seed 42
python -m src.main --track t09 --pipeline src.baselines.t09_consent_only --output out/baseline --run-name baseline --offline --seed 42
python -m src.evaluate --track t09 --pred out/poc/decisions.csv --out reports/metrics_poc.json     # cần gt/ giảng viên
pytest -q
```

- `pytest -q`: **236 passed, 39 skipped** (skip = test cần `gt/` giảng viên), 0 failed. Test của nhóm: `tests/test_t09_policy.py`
  (17), `tests/test_t09_redteam.py` (red-team + regression guards).
- Máy: Windows 11 (10.0.26300), Intel Family 6 Model 213, Python 3.11.9 · git sha khi đo: bf543fe.
- Lưu ý Windows: Makefile dùng `.venv/bin/python`; trên Windows truyền `PY=.venv/Scripts/python`.

## 09 · Limitations & ethics

- **Không tuyên bố tuân thủ.** Gate hỗ trợ DPO/legal rà soát; không kết luận "đạt GDPR / Luật 91". Rule `confidence: low/medium`
  và `needs_legal_review: true` phải được legal xác nhận. GDPR được dùng làm nguyên tắc tham chiếu cho rule `markets: ['*']`;
  điều khoản tương đương ở VN/SG/CA cần legal map.
- **Chưa bao phủ:** EU legitimate interest chưa yêu cầu LIA; sensitive data chỉ FLAG; IN (DPDP) và các market khác → HOLD
  `MARKET_NOT_COVERED`; deletion/retention request chưa mô hình hoá (mốc +24h của đề); allowlist cơ chế chuyển giao có thể thiếu.
- **Hạn chế engine do human review phát hiện** (ghi nhận, **không sửa** vào policy trước khi chấm vì một số đến từ case eval):
  1. `R-GEN-01` coi mọi `ambiguity_notes` không rỗng là ambiguity, kể cả ghi chú thông tin như "luật sắp hiệu lực" (CASE-09)
     → cần phân loại ghi chú (câu hỏi pháp lý vs thông tin/effective_date).
  2. `R-SEC-02` tin khai báo `anonymization` mà không đối chiếu với `raw_retained` → không bắt được mâu thuẫn "blur trước upload
     nhưng vẫn lưu raw trên cloud" (CASE-12).
  3. Không có rule LIA cho EU; tên field LIA không thống nhất giữa các case (`li_assessment_done` ở SG, `lia_done` ở EU) → engine
     ra GO cho CASE-10 mà không đọc bằng chứng LIA; người duyệt phải kiểm thủ công.
  4. Policy xếp `CONSENT_INVALID` vào REWORK, nhưng consent không hợp lệ thực chất là không có căn cứ (HOLD); người duyệt đã nâng
     CASE-03 lên HOLD → nên xem lại mức của rule này ở version sau (dựa trên dev, có thể áp dụng sau khi chấm).
- **False alarm:** fail-safe làm dữ liệu bẩn/thiếu → HOLD; trên fixture sạch không phát sinh false HOLD (dev 0/1), nhưng N GO = 1
  nên chưa đo được đáng tin.
- **Scale 10×:** engine O(case × rule), ~0.01 s/case → không phải vấn đề; nút thắt là thời gian legal review (B = mọi case) →
  cần phân tầng: GO không flag chỉ cần duyệt mẫu.
- **Kế hoạch:** thêm field LIA + rule EU; map điều khoản VN/SG cho rule chung; rule `effective_date` cho DPDP (13/05/2027);
  deletion requirement; property-based test sinh case ngẫu nhiên.

## 10 · Team contribution

**Bảng 2 · Individual contribution evidence** (đối chiếu `git shortlog -sne --all` và `reports/team_contributions.csv`)

| Học viên | Git author/email | Module / vai trò | Commit SHA | Trình bày? |
|---|---|---|---|---|
| Trần Tuấn Anh (26ai.anhtt5@vinuni.edu.vn) | Tran Tuan Anh <76089574+plastma65@users.noreply.github.com> (git, GitHub: plastma65) | Toàn bộ: policy, engine, pipeline, baselines, tests, review loop, report | 822bbaf, dec045f, 74d633e, f6ad721, f7ed6fb, bf543fe, c0fd9d7 (+ commit review evidence) | có |

## 11 · References

| URL | Ngày xem | Ý nghĩa |
|---|---|---|
| https://ketoananpha.vn/luat-bao-ve-du-lieu-ca-nhan-nam-2025 | 2026-11-14 | Toàn văn Luật 91/2025: xác nhận Đ.20 = chuyển xuyên biên giới, Đ.21 = đánh giá tác động xử lý, Đ.24 = trẻ em, Đ.19 = không cần đồng ý |
| https://www.tgu.edu.vn/upload/files/Tailieu-gioithieu-LuatBaovedulieucanhan.pdf | 2026-11-14 | Mục lục luật (đối chiếu chéo tên điều), hiệu lực 01/01/2026 |
| https://eur-lex.europa.eu/eli/reg/2016/679/oj | 2026-11-14 | GDPR: Art. 6 căn cứ, Art. 7 consent, Ch. V chuyển giao, Art. 35 DPIA (dẫn trong rule) |
| `docs/tracks/T09.md` (repo) | 2026-11-14 | Lưu ý pháp lý của đề: VN không có legitimate interest, CCPA không có lawful basis, mốc AI Act / DPDP |

---

**Bảng 3 · Checklist chốt khi nộp**

- [x] Link repo và tag trong README (mục "Bài nộp G04"); input mẫu không chứa PII thật (fixture synthetic CC0). Tag `demo-180m` và `final-24h` đã gắn.
- [x] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [x] Có baseline/PoC metrics (dev self-check), ≥1 failure case, reviewer evidence (`reports/review_evidence/`), decision log.
- [x] Có license/source provenance, data splits, seed, offline environment, GT/eval N.
- [x] Mỗi thành viên có ≥1 commit thực chất và được nêu minh bạch trong bảng trên.
