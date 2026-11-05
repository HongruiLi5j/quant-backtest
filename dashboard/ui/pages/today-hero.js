/* ==========================================================
 * 文件: ui/pages/today-hero.js（D91 / R6-A，预览，不是默认）
 * 用途: 今日页「答案优先」首屏的预览版：一行给出「明天怎么做」——组合目标仓位（大字）+ 动作徽章 + 一句原因 +
 *       今天买卖各几只（点了去信号页）+ 数据新鲜度小标签；其余内容照旧排在下面。
 *   - 开关：今日页顶部「试用答案优先首屏」，记在本机 localStorage（qb-ux-today-hero）；默认关闭，所有者决定要不要当默认
 *   - 所有数字都来自页面已经请求过的接口（summary.strategies 的组合策略、已发布清单），不新增请求、不重算
 * ========================================================== */
import { el, store } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { actBadge, ratio, pageHref, tr } from './U1-common.js';

const KEY = 'qb-ux-today-hero';
const ORDER = [['卖出', 'sell'], ['减仓', 'reduce'], ['买入', 'buy'], ['加仓', 'add']];   // 风险优先，和「今日动作」卡同序

export const heroOn = () => store.get(KEY) === '1';

/** 开关按钮：放在今日页最上面；点了切换并重画 */
export function heroToggle(onChange) {
  const on = heroOn();
  const btn = el('button', { class: 'btn btn--sm btn--ghost u1-hero-toggle', type: 'button', 'aria-pressed': String(on) }, t(on ? 'ux.hero.toggle_off' : 'ux.hero.toggle_on'));
  btn.addEventListener('click', () => { store.set(KEY, on ? '0' : '1'); onChange(); });
  return btn;
}

/** 基准品种的显示名：中文界面用数据里的中文名；英文界面写「代码 (英文名)」，没有英文名就只写代码（revquant：英文里不出现中文名） */
function benchLabel(code, name) {
  if (lang() !== 'en') return name || code;
  const en = t(`ux.hero.bench.${code}`);
  return en === `ux.hero.bench.${code}` ? code : `${code} (${en})`;
}

/** 首屏一行。lead = 组合策略（summary.strategies 里的 blend2）；list = 已发布清单（个人模式才有，可为 null） */
export function heroRow({ s, lead, list, mainKey, warn, benchName }) {
  if (!lead) return null;
  const counts = list && !list.error && !list.empty ? ORDER.map(([a, tab]) => el('a', { class: 'u1-hero__count', href: pageHref('signals.html', { sg_t: mainKey, sg_k: tab }) },
    // revquant：英文界面动作名也要翻译（tr 走 u1.act 词条）
    el('span', { class: 'muted' }, tr('u1.act', a)), ' ', el('b', { class: 'num' }, String((list.rows ?? []).filter((x) => x.action === a).length)))) : [];
  const fresh = [t('u1.sig.data_date', { d: s.data_date }), list?.data_date && list.data_date !== s.data_date ? t('ux.hero.list_date', { d: list.data_date }) : null].filter(Boolean).join(' · ');
  return el('section', { class: 'card u1-hero', 'aria-label': t('ux.hero.title') },
    el('div', { class: 'u1-hero__main' },
      el('p', { class: 'eyebrow' }, t('ux.hero.title'), ' · ', lead.label),
      // revquant 条件 1：0% 只是基准 ETF 的目标仓位；下面的买卖数是另一份全市场股票清单，两个都写明是什么，免得并排看成一回事
      el('p', { class: 'u1-hero__what' }, t('ux.hero.target', { n: benchLabel(s.main, benchName) })),
      el('div', { class: 'u1-hero__pos' }, el('span', { class: 'u1-hero__big num' }, ratio(lead.next_total)), actBadge(lead.action)),
      lead.reason ? el('p', { class: 'u1-hero__why' }, lead.reason) : null,
      el('p', { class: 'ws-note u1-hero__cert' }, t('ux.hero.certainty'))),   // revquant 条件 2：不是有把握的预测
    el('div', { class: 'u1-hero__side' },
      counts.length ? el('p', { class: 'u1-hero__what' }, t('ux.hero.list_label')) : null,
      counts.length ? el('div', { class: 'u1-hero__counts' }, ...counts) : null,
      el('span', { class: `u1-hero__fresh ${warn ? 'is-warn' : 'muted'}` }, fresh),
      el('p', { class: 'ws-note' }, t('ux.hero.preview'))));
}
