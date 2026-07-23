#!/usr/bin/env bash
#
# Скилл «Разведка ниши» — установка в Claude Code.
# Кладёт скилл в ~/.claude/skills/razvedka-nishi/ (пользовательский уровень, доступен во всех проектах).
#
# Установка одной строкой:
#   curl -fsSL https://raw.githubusercontent.com/vasin-k-i/razvedka-nishi/main/install.sh | bash
#
# Или из локальной копии репозитория:
#   bash install.sh
#
set -euo pipefail

REPO_TARBALL="${RN_TARBALL:-https://github.com/vasin-k-i/razvedka-nishi/archive/refs/heads/main.tar.gz}"
SKILL_NAME="razvedka-nishi"
SKILLS_DST="${HOME}/.claude/skills"

find_local_src() {
  # если скрипт лежит рядом с папкой skills/razvedka-nishi — ставим из неё (локальный clone)
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  [ -f "${here}/skills/${SKILL_NAME}/SKILL.md" ] && { echo "${here}/skills/${SKILL_NAME}"; return 0; }
  return 1
}

SRC=""
if SRC="$(find_local_src 2>/dev/null)"; then
  echo "Ставлю из локальной копии: ${SRC}"
else
  command -v curl >/dev/null 2>&1 || { echo "✗ нужен curl" >&2; exit 1; }
  command -v tar  >/dev/null 2>&1 || { echo "✗ нужен tar"  >&2; exit 1; }
  CURL="curl -fsSL"; curl -V 2>/dev/null | grep -qi schannel && CURL="${CURL} --ssl-no-revoke"
  TMP="$(mktemp -d)"; trap 'rm -rf "${TMP}"' EXIT
  echo "Скачиваю скилл «Разведка ниши»…"
  ${CURL} "${REPO_TARBALL}" -o "${TMP}/rn.tar.gz"
  tar -xzf "${TMP}/rn.tar.gz" -C "${TMP}"
  SRC="$(dirname "$(find "${TMP}" -path "*/skills/${SKILL_NAME}/SKILL.md" | head -1)")"
  [ -n "${SRC}" ] && [ -f "${SRC}/SKILL.md" ] || { echo "✗ не нашёл скилл в архиве" >&2; exit 1; }
fi

mkdir -p "${SKILLS_DST}"
rm -rf -- "${SKILLS_DST}/${SKILL_NAME}"
cp -R -- "${SRC}" "${SKILLS_DST}/${SKILL_NAME}"

echo ""
echo "✓ Скилл установлен: ${SKILLS_DST}/${SKILL_NAME}"
echo ""
echo "Дальше:"
echo "  1. Перезапусти Claude Code (закрой и открой заново)."
echo "  2. Напиши:  /razvedka-nishi   — или просто «сделай разведку ниши <тема>»."
echo "  3. Скилл спросит паспорт проекта и проведёт разведку по фазам."
