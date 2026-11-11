"""
知识库导入脚本: xlsx → MySQL
用法:
    python scripts/import_knowledge.py                          # 使用默认路径
    python scripts/import_knowledge.py --xlsx path/to/file.xlsx  # 指定 xlsx
    python scripts/import_knowledge.py --dry-run                 # 仅预览不写入
"""
import argparse
import asyncio
import hashlib
import logging
import re
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.mysql import AsyncSessionLocal
from src.storage.repository import (
    KnowledgeRepository, CompanyMappingRepository,
    ScenarioRepository, RequiredFieldRepository,
    FollowupRepository, RuleRepository, ScriptTemplateRepository,
)
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SHEET_NAMES = {
    "terminology": "险种口语速查",
    "company": "保司口语映射",
    "scenario": "场景口语",
    "required_fields": "出单必收字段",
    "followups": "典型场景追问",
    "rules": "收集规则",
    "templates": "话术条目模板",
}


def _safe_str(cell_value) -> str:
    if cell_value is None:
        return ""
    return str(cell_value).strip()


def _build_header_map(ws) -> dict:
    """读取第一行构建 {列名: 列索引} 映射，兼容列顺序变化。"""
    header = {}
    for idx, cell in enumerate(ws[1]):
        if cell.value:
            header[str(cell.value).strip()] = idx
    return header


def _cell(row, header: dict, name: str) -> str:
    """按列名取单元格值，找不到则返回空字符串。"""
    idx = header.get(name)
    if idx is not None and idx < len(row):
        return _safe_str(row[idx])
    return ""


async def import_terminology(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0
    direct_types = {
        "第三者责任险", "不计免赔", "车上人员责任险",
        "驾乘意外险", "车身划痕险", "车辆损失险",
        "盗抢险", "玻璃单独破碎险", "自燃损失险",
        "发动机涉水险",
    }
    for row in ws.iter_rows(min_row=2, values_only=True):
        slang_cell = row[0] if len(row) > 0 else None
        meaning = _cell(row, h, "标准含义")
        combo = _cell(row, h, "对应险种组合")

        if not slang_cell:
            continue

        slangs = re.split(r"\s*/\s*", str(slang_cell))

        if meaning in direct_types:
            standard = meaning
        elif combo:
            standard = combo
        elif meaning:
            standard = meaning
        else:
            standard = slangs[0].strip()

        for s in slangs:
            s = s.strip()
            if s:
                await KnowledgeRepository.upsert(
                    session, standard_term=standard, alias=s, category="insurance_type",
                )
                count += 1
    return count


async def import_company(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0
    existing = set()

    for row in ws.iter_rows(min_row=2, values_only=True):
        slang_cell = row[0] if len(row) > 0 else None
        full_name = _cell(row, h, "保司")
        remark = _cell(row, h, "备注")

        if not slang_cell or not full_name:
            continue

        for s in re.split(r"\s*/\s*", str(slang_cell)):
            s = s.strip()
            if s and s not in existing:
                existing.add(s)
                await CompanyMappingRepository.upsert(session, slang=s, full_name=full_name, remark=remark)
                await KnowledgeRepository.upsert(session, standard_term=full_name, alias=s, category="company")
                count += 1
    return count


async def import_scenario(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        slang = _cell(row, h, "口语")
        meaning = _cell(row, h, "含义")

        if not slang or not meaning:
            continue

        await ScenarioRepository.upsert(session, slang=slang, description=meaning)

        for s in re.split(r"\s*/\s*", slang):
            s = s.strip()
            if s:
                await KnowledgeRepository.upsert(session, standard_term=meaning, alias=s, category="scenario")
        count += 1
    return count


async def import_required_fields(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        group = _cell(row, h, "分组")
        field_name = _cell(row, h, "字段")
        required = _cell(row, h, "必填")
        description = _cell(row, h, "说明")
        common_say = _cell(row, h, "业务员常见说法")
        sort_str = _cell(row, h, "#")

        if not field_name:
            continue

        try:
            sort_order = int(sort_str) if sort_str else count
        except ValueError:
            sort_order = count

        await RequiredFieldRepository.create(
            session,
            group_name=group,
            sort_order=sort_order,
            field_name=field_name,
            required=required,
            description=description,
            common_say=common_say,
        )
        count += 1
    return count


async def import_followups(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        agent_say = _cell(row, h, "业务员原话")
        obtained = _cell(row, h, "已获得")
        missing = _cell(row, h, "缺的必收项")
        followup = _cell(row, h, "智能体追问示例")

        if not agent_say:
            continue

        await FollowupRepository.create(
            session,
            agent_say=agent_say,
            obtained=obtained,
            missing=missing,
            followup=followup,
        )
        count += 1
    return count


async def import_rules(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        rule = _cell(row, h, "规则")
        sort_str = _cell(row, h, "#")

        if not rule:
            continue

        try:
            sort_order = int(sort_str) if sort_str else count
        except ValueError:
            sort_order = count

        await RuleRepository.create(session, sort_order=sort_order, rule_text=rule)
        count += 1
    return count


async def import_templates(session, ws) -> int:
    h = _build_header_map(ws)
    count = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        code = _cell(row, h, "编号")

        if not code:
            continue

        await ScriptTemplateRepository.upsert(
            session,
            code=code,
            intent=_cell(row, h, "意图"),
            category=_cell(row, h, "分类"),
            security=_cell(row, h, "密级(内部/对外)"),
            insurance_type=_cell(row, h, "适用险种"),
            agent_questions=_cell(row, h, "业务员问法(≥3种,分号分隔)"),
            info_needed=_cell(row, h, "所需信息"),
            followup=_cell(row, h, "追问话术"),
            standard_answer=_cell(row, h, "标准回答"),
            customer_version=_cell(row, h, "可转客户口径"),
            data_deps=_cell(row, h, "数据依赖"),
            prohibited=_cell(row, h, "禁止回答"),
            escalate_condition=_cell(row, h, "转人工条件"),
            adversarial=_cell(row, h, "对抗样例"),
            basis=_cell(row, h, "依据文件与生效日期"),
            source=_cell(row, h, "来源"),
        )
        count += 1
    return count


async def clear_all(session):
    from src.storage.models import KnowledgeTermModel
    from sqlalchemy import delete
    await session.execute(delete(KnowledgeTermModel))
    await CompanyMappingRepository.delete_all(session)
    await ScenarioRepository.delete_all(session)
    await RequiredFieldRepository.delete_all(session)
    await FollowupRepository.delete_all(session)
    await RuleRepository.delete_all(session)
    await ScriptTemplateRepository.delete_all(session)
    await session.flush()


def collect_xlsx_files(paths) -> list[Path]:
    """展开传入的文件/目录为去重后的 .xlsx 文件列表。"""
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw).expanduser()
        candidates = sorted(p.glob("*.xlsx")) if p.is_dir() else [p]
        for c in candidates:
            c = c.resolve()
            if c.suffix.lower() == ".xlsx" and c not in seen:
                seen.add(c)
                result.append(c)
    return result


def default_xlsx_sources() -> list[str]:
    """默认导入源：优先取 .env 的 KNOWLEDGE_BASE_DIR，否则用项目 db 目录。"""
    kb_dir = settings.knowledge.KNOWLEDGE_BASE_DIR
    if kb_dir:
        return [str(kb_dir)]
    return [str(Path(__file__).resolve().parent.parent / "db")]


def compute_fingerprint(files: list[Path]) -> str:
    """基于每个单元格内容计算内容指纹（内容级，任何改动都能被检测）。"""
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.name.encode("utf-8"))
        wb = openpyxl.load_workbook(str(f), read_only=True, data_only=True)
        for ws in wb.worksheets:
            h.update(ws.title.encode("utf-8"))
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    h.update(("" if cell is None else str(cell)).encode("utf-8"))
                    h.update(b"\x00")  # 分隔符，避免相邻单元格拼接产生歧义
        wb.close()
    return h.hexdigest()


async def auto_sync_knowledge(sources: list[str] | None = None) -> bool:
    """启动时自动同步知识库：检测 xlsx 变化，变化则清空后重导（幂等）。"""
    from sqlalchemy import select
    from src.storage.models import KnowledgeImportStateModel

    files = collect_xlsx_files(sources or default_xlsx_sources())
    if not files:
        logger.warning("未在知识库目录找到任何 .xlsx 文件，跳过自动导入")
        return False

    fingerprint = compute_fingerprint(files)

    async with AsyncSessionLocal() as session:
        state = (await session.execute(
            select(KnowledgeImportStateModel).order_by(KnowledgeImportStateModel.id.desc()).limit(1)
        )).scalar_one_or_none()
        stored = state.fingerprint if state else None

    if stored == fingerprint:
        logger.info("知识库 xlsx 无变化 (指纹 %s…)，跳过自动导入", fingerprint[:8])
        return False

    async with AsyncSessionLocal() as session:
        await clear_all(session)
        await session.commit()
    logger.info("检测到知识库 xlsx 变化，已清空旧数据，开始重新导入")

    for f in files:
        await run_import(str(f))

    async with AsyncSessionLocal() as session:
        state = (await session.execute(
            select(KnowledgeImportStateModel)
        )).scalars().first()
        if state:
            state.fingerprint = fingerprint
            state.file_count = len(files)
        else:
            session.add(KnowledgeImportStateModel(fingerprint=fingerprint, file_count=len(files)))
        await session.commit()

    logger.info("知识库自动导入完成 (文件数 %d)", len(files))
    return True


async def run_import(xlsx_path: str, dry_run: bool = False):
    path = Path(xlsx_path)
    if not path.exists():
        logger.error("xlsx 文件不存在: %s", path)
        return

    logger.info("读取 xlsx: %s", path)
    wb = openpyxl.load_workbook(str(path), data_only=True)
    available_sheets = set(wb.sheetnames)

    sheet_tasks = [
        ("terminology", "险种口语速查", import_terminology),
        ("company", "保司口语映射", import_company),
        ("scenario", "场景口语", import_scenario),
        ("required_fields", "出单必收字段", import_required_fields),
        ("followups", "典型场景追问", import_followups),
        ("rules", "收集规则", import_rules),
        ("templates", "话术条目模板", import_templates),
    ]

    for key, sheet_name, import_fn in sheet_tasks:
        if sheet_name not in available_sheets:
            logger.warning("Sheet [%s] 不存在，跳过", sheet_name)
            continue
        logger.info("解析 Sheet: %s", sheet_name)

        if dry_run:
            row_count = ws.max_row - 1 if (ws := wb[sheet_name]) else 0
            logger.info("  [DRY-RUN] 共 %d 行数据，未实际写入", max(row_count, 0))
        else:
            async with AsyncSessionLocal() as session:
                try:
                    count = await import_fn(session, wb[sheet_name])
                    await session.commit()
                    logger.info("  导入 %d 条记录", count)
                except Exception as e:
                    await session.rollback()
                    logger.error("  导入失败: %s", e)

    wb.close()

    if not dry_run:
        logger.info("知识库导入完成!")
    else:
        logger.info("DRY-RUN 完成，未实际写入数据库。")


def main():
    parser = argparse.ArgumentParser(description="知识库 xlsx → MySQL 导入工具")
    parser.add_argument(
        "--xlsx", type=str, nargs="*", default=None,
        help="xlsx 文件或目录，可传多个（默认: 知识库目录，见 .env 的 KNOWLEDGE_BASE_DIR）",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="导入前清空所有知识库表",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅预览，不实际写入数据库",
    )
    args = parser.parse_args()

    sources = args.xlsx or default_xlsx_sources()
    files = collect_xlsx_files(sources)
    if not files:
        logger.error("未找到任何 xlsx 文件: %s", sources)
        return

    logger.info("待导入文件: %s", [str(f) for f in files])

    async def main_async():
        if args.clear and not args.dry_run:
            async with AsyncSessionLocal() as session:
                await clear_all(session)
                await session.commit()
                logger.info("已清空所有知识库表")
        for f in files:
            await run_import(str(f), dry_run=args.dry_run)

    asyncio.run(main_async())


if __name__ == "__main__":
    main()