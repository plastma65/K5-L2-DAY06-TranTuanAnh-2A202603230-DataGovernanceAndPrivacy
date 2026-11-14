# Red-team review — T09 policy engine (`g-t09-1.0`)

> Bài tập học tập, **không phải tư vấn pháp lý**. Reviewer độc lập, chỉ tạo 2 file: `tests/test_t09_redteam.py` và file này.
> Phạm vi đọc: `docs/tracks/T09.md`, `README.md` §2/§6, `policies/t09_policy_v1.yaml`, `src/t09/policy_engine.py`,
> `src/pipeline.py`, `src/common/{review,decision}.py`, `tests/test_t09_policy.py`, `reports/dev_labels_t09.csv`,
> `fixtures/.../rules/policy_example.yaml`, CASE-01..08 (dev). **Không đọc CASE-09..12, không xem `out/*/decisions.csv`, không đọc `gt/`.**
> Mọi case trong test là dict tổng hợp. Không gọi mạng → số điều luật được đánh giá từ trí nhớ, chỗ nào không chắc đều ghi rõ "không chắc".

Kết quả chạy: 20 phát hiện (5 critical, 12 major, 3 minor), mỗi phát hiện một test `xfail(strict=True)`; 54 test còn lại ghim hành vi đúng đang có.

## 1. Bảng bug

Nguyên nhân gốc chung cho RT-01..04, 06..08: engine **fail-safe với `not_true`** nhưng **fail-open với `is_true`, `in [blocklist]`, và so sánh số**; kiểm tra required field chỉ ở mức key top-level (`f not in case`), không kiểm null, không kiểm key lồng nhau, không kiểm kiểu.

| ID | Mức độ | Input tối thiểu (trên case sạch) | Kết quả hiện tại | Kết quả đúng + căn cứ | Test name | Đề xuất sửa |
|---|---|---|---|---|---|---|
| RT-01 | critical | `market: EU` hoặc `SG`, `transfer.cross_border: true`, `transfer.mechanism: "verbal_agreement"` (hoặc `"n/a"`, `"None"`, `"Unknown"`, `"TBD"`, `0`, `[]`) | GO, không reason code | HOLD `XBORDER_NO_MECHANISM`. GDPR Ch. V chỉ cho chuyển khi có adequacy / safeguard Đ.46 / BCR / derogation Đ.49; giá trị không nhận diện được = chưa có cơ chế. Fail-safe của chính thiết kế. | `test_rt01_unrecognised_transfer_mechanism_is_not_go` | Đổi R-EU-02, R-SG-03 từ blocklist sang allowlist: `{path: transfer.mechanism, op: not_in, value: [adequacy, scc, bcr, ...]}` (chuẩn hoá lower-case trước khi so). |
| RT-02 | critical | `transfer: null` hoặc `{}` hoặc `{"destination": "US"}` (mọi market) | GO | HOLD `EVIDENCE_MISSING`. Không có bằng chứng về chuyển xuyên biên giới ≠ "không chuyển"; đây đúng là failure mode "bỏ sót rủi ro xuyên biên giới" của đề. | `test_rt02_missing_nested_transfer_evidence_is_hold` | Thêm required path lồng nhau (`transfer.cross_border`, `storage.encryption_at_rest`, `consent.obtained`…) và coi `null` là thiếu; hoặc engine tự HOLD khi `missing` của rule market đang xét khác rỗng. |
| RT-03 | critical | `market: EU/VN/SG`, `transfer.cross_border: "true"` (hoặc `"yes"`, `1`, `null`), `destination: "US"` | GO | HOLD. Giá trị không phải boolean là dữ liệu không tin được; `is_true` dùng `x is True` nên đọc thành "không chuyển". | `test_rt03_non_boolean_cross_border_is_not_go` | Validate kiểu ở đầu `evaluate_case`: field boolean mà không phải `bool` → HOLD `EVIDENCE_MISSING` (không tự ép kiểu). |
| RT-04 | critical | `minors_possible: "true"` (hoặc `"yes"`, `1`, `null`, `"unknown"`), mọi market | GO | HOLD `MINORS` hoặc `EVIDENCE_MISSING`. `null` = chưa đánh giá, không phải "không có trẻ em". | `test_rt04_non_boolean_minors_flag_is_hold` | Như RT-03; hoặc viết lại R-GEN-02 thành `{not: {path: minors_possible, op: eq, value: false}}` để chỉ `false` tường minh mới qua. |
| RT-05 | critical | Case rủi ro bất kỳ + `as_of = 2025-12-31` (trước `effective_date: 2026-01-01` của policy) | GO: mọi rule thành ghi chú UPCOMING, kể cả ambiguity, minors, GDPR | HOLD hoặc từ chối chạy. GDPR áp dụng từ 25/05/2018; R-GEN-01 là protocol của lab, không có ngày hiệu lực. `as_of` lấy từ config (`ctx.param("as_of")`) nên gõ nhầm một ngày là toàn bộ gate mở. | `test_rt05_as_of_before_policy_effective_date_is_not_go` | `evaluate_case`/`load` raise khi `as_of < policy.effective_date`; đặt `effective_date` riêng cho từng rule theo luật gốc (GDPR 2018-05-25, Luật 91 2026-01-01) thay vì thừa kế mặc định. |
| RT-06 | major | `retention_days: "9999"` (hoặc `""`, `"forever"`, `"90 days"`, `NaN`, `[]`) | GO | Khác GO (REWORK `RETENTION_UNBOUNDED`/`EXCESSIVE` hoặc HOLD `EVIDENCE_MISSING`). `"forever"` là đúng trường hợp storage limitation muốn bắt. | `test_rt06_non_numeric_retention_is_not_go` | Trong `eval_cond`, toán tử số trên giá trị không phải số không được trả `False` im lặng: gắn cờ evidence invalid → HOLD. Loại `bool` và `NaN`. |
| RT-07 | major | `retention_days: -1` (hoặc `-365`, `true`, `false`) | GO | Khác GO. Số ngày âm / boolean là dữ liệu sai (`bool` là subclass của `int` nên lọt `isinstance`). | `test_rt07_impossible_retention_is_not_go` | Thêm rule `retention_days lt 0` → HOLD/REWORK, và loại `bool` trong nhánh số. (`0` cố ý không test: có thể hiểu là "không lưu".) |
| RT-08 | major | `storage.raw_retained: true`, `storage.location: "Cloud"` / `"cloud_us-east"`, hoặc `anonymization: "None"` / `"no"` / `[]`, hoặc `raw_retained: "true"` / `null` | GO | REWORK `RAW_PII_IN_CLOUD`. Cùng một thực tế (raw chưa ẩn danh trên cloud), chỉ khác cách viết. | `test_rt08_raw_pii_in_cloud_variants_are_not_go` | Chuẩn hoá chuỗi (strip + lower) trước khi so; đảo logic sang allowlist: `location not_in [on_device, on_prem]` và `anonymization not_in [<các giá trị hợp lệ>]`. |
| RT-09 | major | `market: SG`, `lawful_basis: consent`, `consent.granular: false` hoặc `consent.withdrawable: false` | GO | REWORK `CONSENT_INVALID` (nhất quán với EU/VN). PDPA s.14(2)(a) (không được đòi consent vượt mức hợp lý như điều kiện cung cấp dịch vụ), s.16 (rút consent). | `test_rt09_sg_invalid_consent_is_not_go` | Thêm `R-SG-05` sao chép R-EU-04, ref `PDPA s.14(2), s.16`, `confidence: medium`, `needs_legal_review: true`. |
| RT-10 | major | `data_subjects: ["children"]` (hoặc có `minors`, `school_children`), `minors_possible: false` | GO | HOLD `MINORS` (hoặc `AMBIGUOUS_NEEDS_LEGAL`): bằng chứng mâu thuẫn phải lên legal. `data_subjects` chưa bao giờ được engine đọc. | `test_rt10_children_in_data_subjects_is_hold` | Thêm op `contains_any` (hoặc `intersects`) cho list và rule `data_subjects contains_any [children, minors, …]` → HOLD. |
| RT-11 | major (không chắc về mức luật) | `market: VN/SG/US-CA`, `data_types: ["face", "in_cabin_video", "biometric_template"]`, mọi thứ khác sạch | GO, `needs_legal_review = false` | Ít nhất phải gắn cờ legal review. Sinh trắc học là dữ liệu nhạy cảm ở cả 4 market (GDPR Đ.9 khi dùng để định danh; CCPA "sensitive personal information" 1798.140(ae); VN: dữ liệu cá nhân nhạy cảm theo danh mục NĐ 356 — cần đối chiếu). Ảnh mặt trong video **không tự động** là Đ.9 GDPR → chọn mức "flag", không ép HOLD. | `test_rt11_sensitive_data_types_are_at_least_flagged` | Rule `data_types contains_any [face, biometric*, in_cabin_video]` → REWORK/HOLD hoặc chỉ bật `needs_legal_review`; nhóm chốt mức với DPO. |
| RT-12 | major | (a) `cross_border: true`, `mechanism: "SCC"`, `destination: null`; (b) `cross_border: false` nhưng `destination: "US"` / `mechanism: "SCC"` | GO | HOLD. (a) Không biết nước nhận thì không đánh giá được adequacy / TIA. (b) Hai trường mâu thuẫn nhau → không tin được cờ `cross_border`. | `test_rt12_contradictory_transfer_block_is_hold` | Rule chung: `cross_border is_true AND destination empty` → HOLD `EVIDENCE_MISSING`; `cross_border not_true AND (destination nonempty OR mechanism nonempty)` → HOLD `AMBIGUOUS_NEEDS_LEGAL`. |
| RT-13 | major | Policy copy có `path: transfer.crossborder` (gõ thiếu `_`) trong R-EU-02; case EU chuyển xuyên biên giới không mechanism | Policy load OK, case → GO; cột `missing_fields` trong checklist có ghi nhưng không ảnh hưởng quyết định | `PolicyError` lúc load, hoặc HOLD `EVIDENCE_MISSING`. Docstring engine tự cam kết "refuse to run rather than silently skip rules". | `test_rt13_misspelled_rule_path_is_detected` | Khai báo `case_schema` (danh sách path hợp lệ) trong YAML và validate mọi `path` lúc load; `anonymization`, `li_assessment_done`, `purpose` là optional có khai báo. |
| RT-14 | major | `apply_review` với `review_note = None` hoặc `NaN`, `edit` → `GO` trên case HOLD | Override được áp dụng (`str(None)` = `"None"`, truthy), HOLD → GO không có lý do | `ValueError`: note là bắt buộc. Đường CSV hiện an toàn nhờ `fillna("")`, nhưng hàm public không tự bảo vệ. | `test_rt14_override_with_null_note_is_refused` | Chuẩn hoá: `note = "" if pd.isna(x) else str(x).strip()` cho cả `review_note` và `override_value`. |
| RT-15 | major | `apply_review` với `reviewer = ""` / `"   "` / `None`, `edit` → `GO` có note | Override được áp dụng; audit log ghi actor rỗng (hoặc `getpass.getuser()` — sai người) | `ValueError`: quyết định legal phải có người đứng tên (README §7, `Decision.owner` cũng bắt buộc). | `test_rt15_override_without_reviewer_is_refused` | Kiểm `reviewer.strip()` ngay trong `apply_review` cho mọi quyết định khác `skip`. |
| RT-16 | major | Hai dòng review cho cùng case: `dpo: escalate`, rồi `intern: edit → GO` | Dòng sau thắng: GO, `approval_status = overridden` | `ValueError` (hoặc giữ HOLD). Hai reviewer mâu thuẫn không được tự phân xử theo thứ tự dòng. | `test_rt16_conflicting_reviews_for_same_case_are_refused` | Raise khi `reviewed["item_id"].duplicated()`; nếu muốn cho phép nhiều vòng, lấy quyết định nghiêm nhất. |
| RT-17 | major | `edit` + `override_value: "REJECT"` (hoặc `FIX`, `RECAPTURE`) trên case HOLD; `PASS`/`ACCEPT`/`RELEASE`/`KEEP` | HOLD → **REWORK** (alias của track khác: `REJECT → REWORK`); `PASS` → GO | `ValueError`: contract T09 chỉ có GO/HOLD/REWORK. Reviewer gõ "REJECT" để chặn lại vô tình hạ mức HOLD xuống REWORK. | `test_rt17_override_value_outside_go_hold_rework_is_refused` | Trong `apply_review` kiểm `ov.upper() in {"GO","HOLD","REWORK"}` trước, không gọi `canonical_status` có alias. |
| RT-18 | minor | Policy copy có `markets: EU` (string thay vì list) | Load OK; so khớp bằng substring (`market "E"` khớp), `_markets_covered` thêm `'E'`, `'U'` | `PolicyError`. | `test_rt18_markets_must_be_a_list` | `load_policy`: `isinstance(r["markets"], list)` và mọi phần tử thuộc `default_markets_covered ∪ {"*"}`. |
| RT-19 | minor | Policy copy có `params.retention_max_days: "365"` | Load OK; `evaluate_case` nổ `TypeError` thô (fail-closed nhưng không rõ nguyên nhân) | `PolicyError` lúc load, thông báo chỉ rõ param. | `test_rt19_non_numeric_param_is_rejected_cleanly` | Validate kiểu của `value`/`param` cho op `gt/gte/lt/lte` trong `_validate_cond`. Khi sửa nhớ bỏ cả `raises=TypeError` trong marker. |
| RT-20 | minor | Xoá hẳn key `ambiguity_notes` | GO | HOLD `EVIDENCE_MISSING`: "chưa đánh giá ambiguity" ≠ "không có ambiguity". `""` tường minh vẫn là GO. | `test_rt20_absent_ambiguity_notes_is_evidence_missing` | Thêm `ambiguity_notes` vào `required_case_fields` (8 case dev đều có key này). |

**Thứ tự sửa đề xuất:** RT-02/03/04 (một lớp validate schema + kiểu ở đầu `evaluate_case` sẽ đóng cả ba, kéo theo RT-06/07/12/20) → RT-01 (allowlist) → RT-05 → RT-14..17 (`apply_review`) → phần còn lại.
Lưu ý protocol: các sửa trên đều xuất phát từ case tổng hợp + nguyên tắc fail-safe, không từ split eval. Nhưng sửa rule sau khi đã chạy eval vẫn là đổi policy sau khi "frozen" → nên bump `policy_version` (vd `g-t09-1.1`), ghi trong report và báo cả hai số.

### Quan sát thêm (không viết test)

- **Căn cứ pháp lý khai báo mà không có bằng chứng**: `lawful_basis: contract` / `public_task` / `vital_interest` / `legal_obligation` → GO ở EU, VN, SG dù `consent.obtained: false` và chủ thể là người đi đường (không có hợp đồng nào với họ). Cùng họ với hạn chế LIA đã biết: engine tin nhãn `lawful_basis`. Tối thiểu nên bật `needs_legal_review` cho mọi basis khác `consent`.
- **R-EU-AIA-01 quá rộng**: điều kiện `purpose nonempty` → sau 02/12/2027 *mọi* case EU sẽ HOLD (false HOLD 100%), còn case thiếu `purpose` thì né được. Reason code tên `..._UPCOMING` mà lại được phát ra khi rule đã có hiệu lực.
- **`decision.json` run-level = GO / `ALL_CASES_SIGNED_OFF`** ngay cả khi các case đã ký đều là HOLD. Đúng theo docstring (GO = "đã ký duyệt xong") nhưng người đọc dễ hiểu là "được phát hành"; nên đổi reason hoặc thêm `n_hold` vào câu `reason`.
- `approve` kèm `override_value: GO` trên case HOLD → override bị bỏ qua im lặng (vẫn HOLD, fail-safe). Nên raise để reviewer biết ý định của họ không được áp dụng.
- Note 1 ký tự (`"."`) vẫn qua kiểm tra "note bắt buộc". Tuỳ nhóm có muốn đặt độ dài tối thiểu.
- Sau override sang GO, `reason_codes` vẫn giữ mã cũ + `HUMAN_OVERRIDE` (vd `DPIA_MISSING;HUMAN_OVERRIDE` với decision GO). Tốt cho audit, nhưng sẽ làm lệch metric reason-code match nếu file đó được đem chấm.

## 2. Bảng trích dẫn

Không tra cứu mạng (theo quy tắc), nên cột đánh giá là đối chiếu từ trí nhớ về văn bản. "Không chắc" = cần mở văn bản gốc trước khi nộp; mình không đưa ra số điều thay thế như một sự thật.

| rule_id | ref hiện tại | Đánh giá | Ghi chú |
|---|---|---|---|
| R-GEN-01 | Lab protocol (T09.md §4) "ambiguity => HOLD" | OK | Đúng nguồn (protocol nội bộ, không phải luật). `confidence: high` hợp lý. `needs_legal_review: true` hợp lý. |
| R-GEN-02 | GDPR Art. 8; Recital 38 (VN: Luật 91 — verify article) | Sai một phần | Art. 8 chỉ nói về consent của trẻ em với *information society services* (app/web), không áp cho camera ADAS quay người đi đường. Recital 38 thì khớp; có thể thêm Art. 6(1)(f) (đoạn "in particular where the data subject is a child") và Art. 35. Rule áp `*` nhưng chỉ dẫn GDPR: US-CA nên dẫn 1798.120(c) (opt-in cho người dưới 16 khi bán/chia sẻ), SG dẫn hướng dẫn PDPC về dữ liệu trẻ em, VN dẫn điều về trẻ em của Luật 91 (mình nhớ là Đ.24 nhưng **không chắc**). Giữ `confidence: medium`, hoặc `low` cho tới khi có ref theo từng market. |
| R-RET-01 | GDPR Art. 5(1)(e) | OK cho EU; thiếu cho market khác | Nội dung khớp. Với `markets: ['*']` nên bổ sung: PDPA s.25 (retention limitation), CCPA 1798.100(a)(3) (công bố thời hạn lưu, không lưu lâu hơn mức cần thiết), nguyên tắc lưu trữ của Luật 91 (số điều **không chắc**, mình nghĩ nằm ở Đ.3). `high` chỉ đúng cho EU → đề xuất `medium`. |
| R-RET-02 | GDPR Art. 5(1)(e); internal retention schedule | OK | Đã ghi rõ 365 ngày là số nội bộ. `confidence: low` đúng. |
| R-SEC-01 | GDPR Art. 32 | OK nhưng confidence quá cao | Art. 32(1)(a) nêu mã hoá là biện pháp "as appropriate", không bắt buộc tuyệt đối → `medium`. Market khác: PDPA s.24 (protection obligation), CCPA 1798.100(e) + 1798.81.5 (reasonable security). |
| R-SEC-02 | GDPR Art. 5(1)(c), Art. 25; EDPB Guidelines 01/2020 | OK | Khớp (minimisation, privacy by design; EDPB 01/2020 khuyến nghị xử lý cục bộ / ẩn danh trước khi rời xe). `medium` hợp lý. Cùng vấn đề rule `*` chỉ dẫn GDPR. |
| R-CON-01 | GDPR Art. 6(1)(a), Art. 7(1); VN Luật 91 Đ.9 | OK | Khớp. Thiếu ref SG (PDPA s.13) dù rule áp cho SG. Đ.9 Luật 91 là điều về sự đồng ý theo trí nhớ của mình và theo `policy_example.yaml`. |
| R-CON-02 | GDPR Art. 7(1); VN Luật 91 Đ.9 | OK | Art. 7(1) đúng là nghĩa vụ chứng minh. Thiếu ref SG; PDPA không có điều "demonstrate consent" tương đương trực tiếp (gần nhất là accountability s.11–12) → với SG nên `medium` + `needs_legal_review: true`. |
| R-EU-01 | GDPR Art. 6(1)(a)-(f) | OK | `high` hợp lý. |
| R-EU-02 | GDPR Chapter V, Art. 44-49 | OK | Ref đúng; vấn đề nằm ở điều kiện (RT-01), không ở trích dẫn. |
| R-EU-03 | GDPR Art. 35(1),(3)(c); EDPB 01/2020 | OK | 35(3)(c) = giám sát có hệ thống khu vực công cộng quy mô lớn, khớp note. `medium` + legal review hợp lý vì "large scale" là đánh giá theo từng case. |
| R-EU-04 | GDPR Art. 7(2), Art. 7(3); Art. 4(11) | OK | 7(3) khớp `withdrawable`. `granular` khớp hơn với 4(11) "specific" + Recital 32/43 (và 7(4) về bundling); 7(2) chỉ nói yêu cầu consent phải tách bạch trong văn bản → nên thêm Recital 43. |
| R-EU-05 | GDPR Art. 13-14 | OK | Người bị quay trực tiếp → Art. 13. `high` hợp lý. |
| R-EU-AIA-01 | AI Act Art. 6, Art. 113 as amended by Reg. 2026/1744 | Không chắc | Art. 6 (phân loại rủi ro cao) và Art. 113 (thời điểm áp dụng) đúng vai trò. Nhưng ADAS là *safety component* của xe → thuộc **Phụ lục I** (luật type-approval xe, Section B), không phải Phụ lục III; theo chính `docs/tracks/T09.md` mốc Phụ lục I là **02/08/2028**, không phải 02/12/2027. Thêm nữa, theo trí nhớ của mình Art. 2(2) AI Act giới hạn phần áp dụng trực tiếp cho sản phẩm Section B. Reg. 2026/1744 mình không kiểm chứng được, chỉ dựa vào đề. `confidence: low` đúng; nên xem lại `effective_date`. |
| R-VN-01 | Luật 91 Đ.9 (consent), Đ.19 (không cần đồng ý) | Không chắc (có vẻ khớp) | Trùng `policy_example.yaml` và trùng trí nhớ của mình. Danh sách `contract / legal_obligation / vital_interest / public_task` là từ vựng GDPR ánh xạ sang Đ.19, không phải nguyên văn → giữ `medium` + legal review. |
| R-VN-02 | Luật 91 Đ.21; NĐ 356/2025 dossier rules | Không chắc — **nghi lệch số điều** | Theo trí nhớ của mình: Đ.20 = chuyển dữ liệu cá nhân xuyên biên giới, Đ.21 = đánh giá tác động xử lý, Đ.22 = cập nhật hồ sơ. Policy (và file mẫu của giảng viên, vốn ghi theo bản dịch luatvietnam) đang để ngược lại. Mình **không khẳng định bên nào đúng**: mở bản tiếng Việt trên thuvienphapluat đối chiếu tiêu đề Đ.20/Đ.21 trước khi nộp. Mốc "60 ngày" khớp trí nhớ. NĐ 356 chưa có số điều → `confidence: low` cho tới khi đối chiếu. |
| R-VN-03 | Luật 91 Đ.20 (DPIA), Đ.22 (update within 6 months) | Không chắc — nghi lệch số điều | Như trên (cặp Đ.20/Đ.21 có thể bị hoán đổi). Lưu ý nội dung: hồ sơ DPIA nộp trong 60 ngày *kể từ khi bắt đầu xử lý*, nên "chưa có DPIA" không hẳn là vi phạm ngay ngày đầu → HOLD là lựa chọn thận trọng của nhóm, nên ghi vào `note`. Policy rút gọn "update within 6 months of a change" (bản mẫu) thành "update within 6 months" → nghĩa khác đi; khôi phục nguyên văn bản mẫu rồi đối chiếu. Đề xuất `low`. |
| R-VN-04 | Luật 91 Đ.9, Đ.10 (withdrawal) | Không chắc (có vẻ khớp) | Trùng file mẫu và trí nhớ (Đ.10 = rút lại sự đồng ý). `medium` hợp lý. |
| R-VN-05 | "Quyền được biết — verify article number" | Không chắc | Chưa có số điều; mình nghĩ quyền được biết nằm trong điều về quyền của chủ thể dữ liệu (Đ.4) nhưng **không chắc**. `confidence: low` + legal review là đúng; không nên để REWORK/HOLD dựa trên ref trống khi chốt bản nộp. |
| R-CA-01 | Cal. Civ. Code 1798.100(a); CCPA Regs §7012 | OK | Khớp (notice at collection; 11 CCR §7012). `high` hợp lý. Ghi chú: quy định CCPA mới về risk assessment (hiệu lực từ 2026) có thể cần một rule kiểu DPIA cho CA — mình không chắc phạm vi, để legal xem. |
| R-SG-01 | PDPA s.13; First/Second Schedule | OK | Nên thêm s.17 (điều dẫn tới các Schedule). `public_task` bị loại khỏi allowlist SG là hợp lý. `medium` hợp lý. |
| R-SG-02 | PDPA First Schedule Part 3 para 1 + assessment | OK | Khớp trí nhớ (ngoại lệ legitimate interests yêu cầu đánh giá trước và phải cho biết việc dựa vào ngoại lệ). `medium` hợp lý. |
| R-SG-03 | PDPA s.26; PDP Regulations 2021 Part 3 | OK | Khớp. `medium` hợp lý. |
| R-SG-04 | PDPA s.20 | OK | Khớp. Lưu ý s.20 gắn với việc thu thập có consent; khi dựa vào legitimate interests nghĩa vụ thông tin đến từ First Schedule → `medium` an toàn hơn `high`. |

Vấn đề chung về trích dẫn: (1) năm rule `markets: ['*']` chỉ dẫn GDPR dù được áp cho VN/SG/US-CA — audit trail của một case VN sẽ ghi "GDPR Art. 32" làm căn cứ; nên cho `source` là list theo market. (2) Mọi rule GDPR đang thừa kế `effective_date: 2026-01-01` của policy (liên quan RT-05). (3) Ref NĐ 356/2025 chưa có số điều ở bất kỳ rule nào.

## 3. Những điểm đúng thiết kế dù trông như bug (đừng sửa nhầm)

Các điểm dưới đây đã có test thường (không xfail) trong `tests/test_t09_redteam.py` để giữ nguyên hành vi:

- `market: "vn"`, `"EU "`, `""`, `null`, `["EU"]` → HOLD `MARKET_NOT_COVERED`. Không tự chuẩn hoá market là lựa chọn fail-safe đúng; nếu muốn giảm false HOLD thì chuẩn hoá ở bước load và ghi audit, đừng nới trong engine.
- `lawful_basis: "Consent"` / `" consent"` / `null` ở EU/VN/SG → HOLD `NO_LAWFUL_BASIS`. False HOLD do định dạng, nhưng đúng hướng an toàn.
- `dpia_done: "true"`, `consent.obtained: "true"`, `transfer_impact_assessment: "yes"`, `consent: null/{}` → HOLD. Nhóm `not_true` fail-safe đúng; **giữ nguyên ngữ nghĩa `x is True`**, chỉ cần sửa phía `is_true` (RT-03/04).
- VN chuyển xuyên biên giới có `mechanism: "SCC"` + consent hợp lệ nhưng chưa có TIA → vẫn HOLD. Ngược lại VN có TIA mà `mechanism: null` → GO: đúng, Luật 91 không dùng khái niệm "mechanism" kiểu GDPR.
- US-CA: `lawful_basis` bất kỳ (kể cả `"Consent"`, `"n/a"`), `dpia_done: false`, cross-border không mechanism → không tạo finding. Đúng với lưu ý của đề.
- `retention_days: 365` → GO, `366` → REWORK (ngưỡng `gt`, số nội bộ). `retention_days: 0` → GO, mình coi là hợp lệ.
- `ambiguity_notes: "   "` hoặc list/dict khác rỗng → HOLD; `[]` hoặc `""` → không HOLD.
- Case EU sạch → GO kèm ghi chú UPCOMING của R-EU-AIA-01 và `needs_legal_review = true`. Đúng yêu cầu "tương lai → ghi chú".
- `reject` không có override và `escalate` → HOLD; `skip` → giữ `pending_legal` nên run-level vẫn HOLD.
- `edit` → `GO` trên case HOLD khi có note và reviewer: được phép (con người là người duyệt cuối), có `HUMAN_OVERRIDE` + audit.
- Run-level HOLD `PENDING_LEGAL_APPROVAL` khi chưa review: đúng, không phải pipeline lỗi.
- Đã biết, không báo lại: EU `legitimate_interest` chưa đòi LIA; notice thiếu = REWORK (chưa chốt). Ý kiến reviewer cho câu đang treo: với EU/VN/SG nên giữ REWORK khi các điều kiện khác đủ (khắc phục được bằng cách bổ sung thông báo, và nhãn dev CASE-05 của nhóm cũng là REWORK); nâng HOLD chỉ khi thiếu notice đi cùng `lawful_basis: consent` — vì consent không thể "informed" nếu chưa thông báo. Đây là ý kiến, chưa phải finding.

## 4. Lệnh đã chạy

```
.venv/Scripts/python -m pytest -o addopts="" -q tests/test_t09_redteam.py
54 passed, 118 xfailed in 5.27s
```

0 failed, 0 xpassed. 118 xfailed = 20 phát hiện (RT-01..RT-20) nhân với các tham số. Đã chạy thêm `--runxfail` để xác nhận mỗi xfail hỏng đúng lý do mong đợi (assert `'GO' == 'HOLD'` / `DID NOT RAISE`), không phải do lỗi trong test.

---

## 5. Trạng thái sửa (cập nhật 2026-11-14, policy g-t09-1.1)

| Finding | Commit | Cách sửa |
|---|---|---|
| RT-02/03/04/06/07/20 | `74d633e` | `case_schema` có kiểu: field vắng / null / sai kiểu → HOLD `EVIDENCE_MISSING` |
| RT-05 | `74d633e` | `as_of` < `effective_date` của policy → `PolicyError` (từ chối chạy, exit 2) |
| RT-13/18/19 | `74d633e` | kiểm tra lúc load: path phải có trong schema, `markets` là list, param số cho op số, regex hợp lệ |
| RT-01 | `74d633e` | allowlist cơ chế chuyển giao (EU Ch. V, SG s.26) thay cho blocklist |
| RT-08 | `74d633e` | so khớp regex không phân biệt hoa thường cho location / anonymization |
| RT-09 | `74d633e` | rule mới R-SG-05 `CONSENT_INVALID` |
| RT-10 | `74d633e` | rule mới R-GEN-03: trẻ em trong `data_subjects` → HOLD `MINORS` |
| RT-11 | `74d633e` | rule mới R-GEN-04 mức `FLAG` (`SENSITIVE_DATA_REVIEW`, bật `needs_legal_review`, không đổi quyết định) |
| RT-12 | `74d633e` | rule mới R-EVD-01/02 → HOLD `EVIDENCE_INCONSISTENT` |
| RT-14..17 | `f6ad721` | `apply_review`: note/reviewer null bị từ chối, dòng review trùng case bị từ chối, override chỉ nhận GO/HOLD/REWORK |
| Trích dẫn | `74d633e` | Luật 91: Đ.20 = chuyển xuyên biên giới, Đ.21 = đánh giá tác động xử lý (đã đối chiếu toàn văn; trước đó bị hoán đổi, file mẫu `policy_example.yaml` của giảng viên cũng hoán đổi), Đ.24 = trẻ em; GDPR Art. 8 → Recital 38; AI Act ADAS = Phụ lục I, 02/08/2028 |

Thay đổi test regression: `test_storage_null_is_not_go` nay mong đợi HOLD (thay vì REWORK) để nhất quán với RT-02.
Không quyết định nào trên 12 case fixture thay đổi giữa g-t09-1.0 và g-t09-1.1 (CASE-03, CASE-04 có thêm cờ `SENSITIVE_DATA_REVIEW`).
Sau sửa: `pytest -q` → 236 passed, 39 skipped, 0 xfailed, 0 failed.
