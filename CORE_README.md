# FilterTool - bản code lõi

## Chức năng

FilterTool là công cụ Python chạy trên máy cá nhân để hỗ trợ systematic
mapping các bài báo về LLM/AI agent trong offensive security. Luồng xử lý
chính:

1. Tìm metadata bài báo qua Semantic Scholar, OpenAlex, Crossref và arXiv.
2. Chuẩn hóa metadata, lưu SQLite và loại bản ghi trùng theo DOI/tiêu đề.
3. Dùng rule và semantic score để sắp xếp candidate cho việc review.
4. Tạo workbook để người nghiên cứu quyết định Include/Exclude/Unsure.
5. Import quyết định và mã hóa thủ công, tính phân tích và xuất report.
6. Ghi provenance/search log, manifest và PRISMA-style flow counts.

Rule/semantic filter chỉ hỗ trợ ưu tiên và review; không tự thay thế quyết
định human. Auto taxonomy là gợi ý, không thay thế manual coding. PRISMA-style
counts và candidate sparse cells cũng không tự khẳng định systematic review
hoàn tất hay research gap đã được xác nhận.

## Nội dung gói

- `src/filtertool/`: CLI, pipeline, search adapters, filter, SQLite storage,
  screening, coding, analysis, provenance và xuất dữ liệu.
- `config.example.yaml`: cấu hình mẫu để bắt đầu.
- `pyproject.toml`: yêu cầu Python và các dependencies.
- `LICENSE`: giấy phép phần mềm.

Gói không kèm database, file trong `output/`, cấu hình cá nhân `config.yaml`,
virtual environment, cache hay bộ dữ liệu. Khi chạy lần đầu, các dependencies
và model semantic (nếu sử dụng) cần được cài/tải riêng.

## Chạy nhanh

Cần Python 3.10 trở lên. Giải nén, mở terminal tại thư mục gốc:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item config.example.yaml config.yaml
filtertool collect --config config.yaml
filtertool prepare-review --config config.yaml
```

Sau khi review workbook, import quyet dinh:

```powershell
filtertool import-screening --config config.yaml output\offensive_security\screening_sheet.xlsx
```

Thay đổi `config.yaml` để chỉnh nguồn tìm kiếm, query, năm và từ khóa. Không
đưa API key hoặc dữ liệu riêng tư vào file chia sẻ.
