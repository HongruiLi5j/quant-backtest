/* 今日页·数据日期醒目显示（A07）：页头一行「数据日期：YYYY-MM-DD（北京时间 15:30 收盘后更新）」，
 * 清单日期早于今天（按 Asia/Shanghai 算）或今天的 K 线还没收盘时，加一条提示条。
 * 只用接口已有的字段（summary.data_date / timezone_label、trade_list.data_date），不新增接口。
 * 项目规则：北京时间 15:30 前的当天 K 线是盘中半天数据，不能当收盘信号用。
 * 所有文本走 textContent（el()），不拼 HTML；词条在 i18n/parts/A07.*.json，
 * 词条还没合并进基础字典时 t() 会原样返回键名，这种行直接丢掉，不把键名露给用户（和 A03 一样）。 */
import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';

const CLOSE_MIN = 15 * 60 + 30;   // A 股收盘后数据更新的时间点：北京时间 15:30，用分钟数比较

/** 现在的北京时间：{ date: 'YYYY-MM-DD', minutes: 当天第几分钟, weekend: 是否周六日 }。now 可传入，便于测试 */
export function shanghaiClock(now = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short',
  }).formatToParts(now).map((p) => [p.type, p.value]));
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
    weekend: parts.weekday === 'Sat' || parts.weekday === 'Sun',
  };
}

/** 是不是真实存在的 YYYY-MM-DD（2026-02-30 这种不算） */
function validDate(d) {
  if (typeof d !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return false;
  const [y, m, day] = d.split('-').map(Number);
  const x = new Date(Date.UTC(y, m - 1, day));
  return x.getUTCFullYear() === y && x.getUTCMonth() === m - 1 && x.getUTCDate() === day;
}

/** 判断要不要提示、提示什么：返回词条列表 [{ key, vars }]，不需要提示返回 []（纯函数）。
 *  只有 A 股（cn）才有“北京时间 15:30”这条规则；其他市场调用方不要调用它。
 *  没有交易日历，所以不判断节假日：周一到周五日期落后今天时，只说“可能休市或更新没跑完”，不下结论。 */
export function bannerLines(dataDate, clock = shanghaiClock()) {
  if (!validDate(dataDate)) return [{ key: 'a07.banner.unknown' }];
  const vars = { d: dataDate, today: clock.date };
  if (dataDate > clock.date) return [{ key: 'a07.banner.future', vars }];   // ISO 日期的字符串大小就是日期先后
  if (dataDate === clock.date) return clock.minutes < CLOSE_MIN ? [{ key: 'a07.banner.intraday', vars }] : [];
  if (clock.weekend) return [{ key: 'a07.banner.stale', vars }, { key: 'a07.banner.weekend', vars }];
  return [{ key: 'a07.banner.stale', vars }, { key: clock.minutes < CLOSE_MIN ? 'a07.banner.before_close' : 'a07.banner.after_close', vars }];
}

/** 词条翻成文字；没合并进字典的（t() 返回键名）丢掉 */
function resolve(lines) {
  return lines.map(({ key, vars }) => {
    const text = t(key, vars);
    return text === key ? '' : text;
  }).filter(Boolean);
}

/** 页头那一行日期。cn：带“15:30 收盘后更新”；其他市场只带时区名 */
export function dateLine(dataDate, { market = 'cn', tz = '' } = {}) {
  let text;
  if (!validDate(dataDate)) text = t('a07.date.unknown');
  else if (market === 'cn') text = t('a07.date.line', { d: dataDate });
  else text = t('a07.date.line_other', { d: dataDate, tz });
  if (text.startsWith('a07.')) text = validDate(dataDate) ? dataDate : '';   // 词条未合并：只显示日期本身
  return el('div', { class: 'u1-datebar num' }, text);
}

/** 提示条；不需要提示（或不是 A 股、或词条未合并）返回 null */
export function dateBanner(dataDate, { market = 'cn', now = new Date() } = {}) {
  if (market !== 'cn') return null;
  const lines = bannerLines(dataDate, shanghaiClock(now));
  const texts = resolve(lines);
  if (!texts.length) return null;
  // D90-5：琥珀色只留给真要留意的情况——收盘后该有今天的数据却没有、日期在未来、日期认不出。
  // 周末、开盘前、盘中看到的是「上一个交易日的清单」，这是正常的，提示照样显示但用中性样式（不再和真正的警告抢眼）
  const warn = lines.some((l) => ['a07.banner.after_close', 'a07.banner.future', 'a07.banner.unknown'].includes(l.key));
  // D90-1：提示条压成一行——第一句当标题（「这是 X 收盘后的清单，不是盘中信号」），其余原因收在 ▸ 里，点开看。
  // 原来两三行全展开，今日页首屏被提示占掉一大半（今日动作卡在 900px 屏幕的 633px 处才开始）
  return el('details', { class: `card u1-note ${warn ? 'is-warn ' : ''}u1-datebanner`, role: 'status' },
    el('summary', {}, texts[0]), ...texts.slice(1).map((s) => el('p', {}, s)));
}

/** D08（C04）：全市场买卖清单（东方财富收盘快照）可能比 ETF 日线（新浪）先到一天。
 *  这时提示条说「今天的数据还没出现」，紧接着清单却是今天的——两句话看起来矛盾。
 *  清单日期更新时补一句，说明两个日期各指什么；只比较接口给的日期字符串，不推断 */
export function listNewerLine(dataDate, listDate) {
  if (!validDate(dataDate) || !validDate(listDate) || listDate <= dataDate) return null;
  const text = t('c08.list_newer', { d: listDate, e: dataDate });
  return text === 'c08.list_newer' ? null : el('p', { class: 'ws-note u1-datelist' }, text);
}

/** 今日页顶部：日期行 + 提示条（有就带）+ 清单日期更新时的说明 */
export function dateNotice(dataDate, opts = {}) {
  return el('div', { class: 'u1-datewrap' }, dateLine(dataDate, opts), dateBanner(dataDate, opts),
    opts.market === 'cn' ? listNewerLine(dataDate, opts.listDate) : null);
}
