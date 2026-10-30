#!/usr/bin/env bash
# Áp một gói giao từ Claude (nằm ở ~/Downloads) vào dự án.
#
# Dùng:  bash tools/apply_pkg.sh dubber_pkg_002
#        (tên gói không kèm đuôi; cần có dubber_pkg_002.tar.gz và dubber_pkg_002.md5)
#
# Vì sao có script này:
#   - Khối lệnh áp gói dài, dán tay vào terminal dễ sai (CACH_LAM_VIEC mục 3.4).
#   - Dự án chưa dùng git (chốt 2026-10-29), nên mỗi file bị ghi đè phải được sao lưu
#     để còn quay lại được.
#
# Các bước: kiểm file có -> kiểm md5 -> kiểm đường dẫn trong gói -> sao lưu file sẽ bị ghi đè
#           -> giải nén -> xoá gói trong Downloads -> in danh sách file mới / ghi đè.
# md5 sai hoặc đường dẫn lạ thì dừng TRƯỚC khi đổi bất kỳ file nào.

set -euo pipefail

NAME="${1:-}"
if [[ -z "$NAME" ]]; then
	echo "LỖI: thiếu tên gói. Ví dụ: bash tools/apply_pkg.sh dubber_pkg_002"
	exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DL="$HOME/Downloads"
PKG="$DL/$NAME.tar.gz"
SUM="$DL/$NAME.md5"

for f in "$PKG" "$SUM"; do
	if [[ ! -f "$f" ]]; then
		echo "LỖI: không thấy $f"
		exit 1
	fi
done

if ! (cd "$DL" && md5sum -c --quiet "$NAME.md5"); then
	echo "LỖI: md5 không khớp. Dừng, chưa đổi file nào. Tải lại gói rồi chạy lại."
	exit 1
fi

mapfile -t FILES < <(tar -tzf "$PKG" | grep -v '/$')
if [[ ${#FILES[@]} -eq 0 ]]; then
	echo "LỖI: gói rỗng."
	exit 1
fi

# Chặn đường dẫn tuyệt đối hoặc có "..": gói chỉ được ghi vào bên trong dự án.
for f in "${FILES[@]}"; do
	case "$f" in
		/*|*..*)
			echo "LỖI: đường dẫn lạ trong gói: $f. Dừng, chưa đổi file nào."
			exit 1
			;;
	esac
done

BACKUP="$ROOT/.backups/$(date +%Y%m%d_%H%M%S)_$NAME"
NEW=()
REPLACED=()
for f in "${FILES[@]}"; do
	if [[ -e "$ROOT/$f" ]]; then
		mkdir -p "$BACKUP/$(dirname "$f")"
		cp -p "$ROOT/$f" "$BACKUP/$f"
		REPLACED+=("$f")
	else
		NEW+=("$f")
	fi
done

tar -xzf "$PKG" -C "$ROOT"
rm -f "$PKG" "$SUM"

echo "Đã áp gói $NAME vào $ROOT"
echo "  File mới (${#NEW[@]}):"
for f in "${NEW[@]}"; do echo "    + $f"; done
echo "  File ghi đè (${#REPLACED[@]}):"
for f in "${REPLACED[@]}"; do echo "    ~ $f"; done
if [[ ${#REPLACED[@]} -gt 0 ]]; then
	echo "  Bản cũ đã sao lưu ở: $BACKUP"
	echo "  Quay lại bản cũ:     cp -rp \"$BACKUP/.\" \"$ROOT/\"  (file mới thì xoá tay)"
fi
echo "  Đã xoá $NAME.tar.gz và $NAME.md5 khỏi Downloads."