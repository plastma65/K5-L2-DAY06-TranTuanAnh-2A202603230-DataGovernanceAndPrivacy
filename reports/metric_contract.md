# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py`. File này là nơi nhóm **cam kết** cách dùng chúng.
> Không đổi testset, threshold hay trọng số sau khi đã thấy kết quả eval.

| Mục | Khai báo của nhóm |
|---|---|
| Track | T09 · Multi-Market Privacy Decision Gate |
| Primary metric (tên đúng như evaluator in ra) | `Critical-miss Rate` — chỉ hợp lệ khi `Guardrail: False HOLD rate <= 0.5` = PASS |
| Tăng tốt hay giảm tốt? | lower |
| Đơn vị đánh giá | case (1 dòng `decisions.csv` / case) |
| Công thức + numerator / denominator | số case gold ≠ GO mà ta cho GO / số case gold ≠ GO. Guardrail: số case gold = GO mà ta không cho GO / số case gold = GO |
| Cách ghép prediction ↔ GT | theo `case_id` (exact); case thiếu trong `decisions.csv` = GO (worst case); `case_id` trùng → evaluator báo lỗi |
| Candidate pool cố định | 12 case trong `fixtures/t09_privacy_gate/cases/` (sha256 kiểm theo `manifest.csv`) |
| Review budget B / K | B = 12 (tất cả case đều cần người phụ trách legal phê duyệt cuối); thứ tự ưu tiên: HOLD > REWORK > GO, sau đó ambiguity, `needs_legal_review`, số rule fired |
| Seed | 42 (engine deterministic, không dùng random; `as_of` cố định = 2026-11-14) |
| Threshold policy (chọn trên dev như thế nào) | Không có threshold số học, trừ `retention_max_days: 365` (internal policy, cần DPO xác nhận). Rule viết từ văn bản luật + 8 case dev (CASE-01..08), policy `g-t09-1.0` đóng băng trước khi xem output eval |
| Eval split (không dùng để tune) | `split: eval` = CASE-09..12 (số chính thức); dev CASE-01..08 chỉ dùng tự kiểm với nhãn tự gán `reports/dev_labels_t09.csv` |
| Chỉ số lỗi xấu | **false GO trên case rủi ro** (critical miss) là lỗi nghiêm trọng nhất; false HOLD là lỗi phụ (tốn công legal) |
| Slices sẽ báo cáo (≥2) | theo `gold_decision` (HOLD / REWORK / GO — evaluator in sẵn) và theo `market` (VN / EU / US-CA / SG / ngoài phạm vi) |
| Cách báo độ bất định khi N nhỏ | N eval = 4 → báo **từng case** + Wilson 95% CI cho tỉ lệ; không kết luận chung từ 1 tỉ lệ |
| Secondary | High-risk HOLD Recall, Exact decision accuracy, Reason-code match (risky), audit trace completeness (mọi case có rule_id + source hoặc lý do GO) |
| Baseline | (1) consent-only: "có consent thì GO" (`src/baselines/t09_consent_only.py`) · (2) trivial all-HOLD (`src/baselines/t09_all_hold.py`) |
| Ngày giờ chốt + người chốt | 2026-11-14 · ____ (điền tên người chốt) |

Nếu baseline = 0 thì ghi rõ "baseline = 0" thay vì phần trăm cải thiện.
