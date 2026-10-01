# FilterTool

FilterTool hỗ trợ systematic mapping về việc dùng LLM/AI agent để thực hiện offensive security: penetration testing, khai thác lỗ hổng, CTF và các tác vụ liên quan. Tool chỉ thu thập và sàng lọc ứng viên; người nghiên cứu quyết định inclusion, mã hóa cuối cùng và xác minh mọi candidate sparse cell.

Source code is licensed under MIT (see [LICENSE](LICENSE)). Use of the Semantic Scholar API is separately subject to its API terms and rate limits; the software license does not grant API access.

## Tính năng (Theo yêu cầu)
- **Local & Miễn phí**: Chạy hoàn toàn trên máy cá nhân, không phụ thuộc LLM/API trả phí.
- **Search Multi-source**: Tích hợp Semantic Scholar, OpenAlex, Crossref và arXiv. arXiv có preprint, không mặc nhiên đã peer-review.
- **Deduplication**: Xóa trùng lặp dựa trên DOI và fuzzy matching Title.
- **Rule-based Filter**: Candidate phải khớp ít nhất một LLM/agent term và một offensive-security term; các trường hợp còn lại được giữ cho human review, không bị loại tự động.
- **Semantic Filter**: Sentence Transformers chỉ ưu tiên review; semantic score không phải quyết định include/exclude.
- **Human Screening**: Xuất title/abstract và full-text workbook; import Include/Exclude/Unsure cùng reason code.
- **Manual Coding**: Auto labels là gợi ý riêng. Analysis chỉ dùng paper được người duyệt include và manual codes.
- **PRISMA/Provenance**: Ghi search log theo source/query/cache/config, run manifest, flow counts và citation expansion theo seed.
- **Candidate Sparse Cells**: Cross-tab kỹ thuật tối ưu × tác vụ tấn công và × mục tiêu tối ưu; ô thưa không phải research gap đã xác nhận.

## Cài đặt (Khuyên dùng Môi trường cô lập - Virtual Environment)

Để tránh xung đột thư viện và cài đặt mọi thứ trên ổ E, bạn nên tạo một môi trường Python ảo (venv) riêng cho tool này.

1. Mở PowerShell tại thư mục dự án (nơi có `pyproject.toml`).
2. Tạo config local từ mẫu (không commit file local này):
  ```powershell
  Copy-Item config.example.yaml config.yaml
  ```
3. Tạo môi trường ảo (tên là `venv`):
   ```powershell
   python -m venv venv
   ```
4. Kích hoạt môi trường ảo:
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```
   *(Lưu ý: Nếu bị lỗi Execution Policy, hãy chạy lệnh này trước: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`)*
5. Khi thấy chữ `(venv)` hiện lên ở đầu dòng lệnh, tiến hành cài đặt:
   ```powershell
   pip install -e .
   ```

Dependencies được cài trong thư mục `venv` của project, không ảnh hưởng đến Python hệ thống.

### Semantic Scholar API key (optional)
The key is read from `SEMANTIC_SCHOLAR_API_KEY`; keep it out of `config.yaml`, `.bat` files, and Git. In PowerShell, set it for the current session before running the tool:
```powershell
$env:SEMANTIC_SCHOLAR_API_KEY = "<your-new-key>"
```
For double-click runs, add the variable through Windows User Environment Variables. Open only one FilterTool instance at a time: adaptive pacing is shared across Semantic Scholar endpoints within that process.

### Request pacing
Hosts without a configured floor start without an artificial delay and slow down on transient errors or HTTP 429 responses; successful requests gradually restore speed. The example config enforces at least 1 second between all Semantic Scholar endpoints (the introductory API-key limit) and 3 seconds for the arXiv export API. `Retry-After` is honored. The limiter is shared within one process, so do not run concurrent copies with the same API key.

## Cách sử dụng

Tool cung cấp CLI (Command Line Interface) với lệnh `filtertool`. Mọi cấu hình (từ khóa, nguồn search, trọng số filter) đều nằm trong file `config.yaml`.

### 1. Chạy tách bước tại local
Thu thập độc lập, không lọc hay xuất quyết định:
```bash
filtertool collect --config config.yaml --no-cache
```
`raw_search_results.jsonl` lưu từng Paper record từ adapter trước dedup/filter; đây là metadata đã chuẩn hóa, không phải nguyên văn response JSON/XML của API.

Xử lý dữ liệu đã thu thập để tạo screening workbook, không gọi nguồn tìm kiếm:
```bash
filtertool prepare-review --config config.yaml
```

Cập nhật report sau khi nhập human decisions/manual codes:
```bash
filtertool report --config config.yaml
```

Hoặc dùng shortcut chạy collect rồi prepare-review:
```bash
filtertool run --config config.yaml --no-cache
```
`--no-cache` dùng response trực tiếp từ nguồn và ghi cache_used=false/true vào `search_log.csv`. Lỗi truy vấn làm run manifest đánh dấu invalid; kiểm tra manifest trước khi dùng số liệu.

### 2. Human screening
Full run tạo `screening_sheet.xlsx`. Điền `human_decision` (Include/Exclude/Unsure) và reason code IC*/EC*, rồi import:
```bash
filtertool import-screening --config config.yaml output/offensive_security/screening_sheet.xlsx
```
Lệnh import tạo `fulltext_screening_sheet.xlsx` cho paper Include ở vòng title/abstract. Điền quyết định full-text và `fulltext_retrieved` (yes/no), rồi import workbook này.

### 3. Manual coding
`classified_papers.xlsx` tách cột auto/manual. Chỉ paper đã human-include xuất hiện. Điền các cột `*_manual`, `baseline_compared`, `measured` rồi import:
```bash
filtertool import-coding --config config.yaml output/offensive_security/classified_papers.xlsx
filtertool agreement --config config.yaml
```

### 4. Chạy từng bước (Single Stage)
Nếu bạn bị ngắt quãng hoặc muốn chạy lại một bước cụ thể:
```bash
filtertool run --config config.yaml --stage search
filtertool run --config config.yaml --stage dedup
filtertool run --config config.yaml --stage semantic_filter
filtertool run --config config.yaml --stage analysis
```

Các bước hợp lệ: `search`, `normalize`, `dedup`, `rule_filter`, `semantic_filter`, `verification`, `citation_expansion`, `classification`, `analysis`, `export`.

### 5. Các lệnh hữu ích khác
- **Xem trạng thái database hiện tại (số lượng paper qua từng vòng):**
  ```bash
  filtertool status --config config.yaml
  ```
- **Chỉ xuất danh sách và bảng phân loại (không chạy lại pipeline):**
  ```bash
  filtertool export --config config.yaml
  ```
- **Reset database về trạng thái ban đầu:**
  ```bash
  filtertool reset --config config.yaml
  ```
- **Import data từ file CSV có sẵn:**
  ```bash
  filtertool import-csv --config config.yaml data.csv
  ```
- **Tạo screening workbook:** `filtertool screening-sheet --config config.yaml --stage title_abstract`
- **Xuất PRISMA flow counts:** `filtertool prisma --config config.yaml`
- **Pilot recall** (mỗi dòng trong file là một title kỳ vọng): `filtertool pilot --config config.yaml known_titles.txt`

## Kết quả xuất ra
Nằm trong thư mục `output/` (hoặc đường dẫn bạn cấu hình trong yaml):
- `search_log.csv` và `run_manifest.json`: provenance, cache, lỗi, hash cấu hình và trạng thái run.
- `raw_search_results.jsonl`: các source records đã chuẩn hóa trước dedup/filter.
- `screening_sheet.xlsx` / `fulltext_screening_sheet.xlsx`: quyết định human cùng reason codes.
- `auto_suggestions.xlsx`: taxonomy gợi ý cho toàn bộ candidate; không phải human decision hay final inclusion.
- `classified_papers.xlsx`: auto suggestions và manual coding columns.
- `prisma_counts.csv` / `prisma_counts.json`: flow counts từ search log, audit decisions và status.
- `crosstab.xlsx`: hai cross-tab cùng sheet `Candidate Sparse Cells` và `Validation Search`; sparse cell chưa phải gap đã xác nhận.
- `all_papers.json`: toàn bộ paper metadata và audit trail.

Giới hạn coverage: project chưa tích hợp IEEE Xplore, ACM Digital Library, Scopus hoặc Google Scholar. DBLP adapter có trong code nhưng hiện không bật do endpoint trả challenge trong môi trường chạy trước đó.

Xem [FILE_GUIDE.txt](FILE_GUIDE.txt) để biết chức năng và loại thông tin của từng file trong dự án.
