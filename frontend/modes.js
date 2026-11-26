import {language, translate} from './i18n.js';

export const MODES={
  dax:{
    title:'DAX 助手',icon:'sparkles',historyIcon:'chat',
    headline:'需要计算什么指标？',
    subtitle:'描述指标的计算口径，或粘贴一段需要解释、优化的 DAX。',
    placeholder:'描述你想计算的业务指标，或粘贴一段 DAX…',
    cards:[
      ['formula','生成指标','根据业务需求编写度量值','请基于当前模型，帮我编写一个度量值：'],
      ['book','解释 DAX','说明公式和筛选上下文','请解释下面的 DAX，重点说明筛选上下文：\n'],
      ['bolt','优化公式','检查计算逻辑和性能','请检查并优化以下 DAX，保持业务含义不变：\n'],
    ],
  },
  mquery:{
    title:'Power Query',icon:'clean',historyIcon:'clean',
    headline:'需要怎样转换数据？',
    subtitle:'在输入框下方选择目标查询，再描述需要调整的转换步骤。',
    placeholder:'描述你想对当前查询做出的清洗或转换…',
    cards:[
      ['clean','清洗查询','保留数据源，整理转换步骤','请基于当前查询清洗数据，保留原有数据源：'],
      ['book','解释步骤','理解每一步的数据变化','请解释当前 Power Query 的每一个转换步骤。'],
      ['bolt','优化查询','减少重复步骤，改善可维护性','请检查当前查询并给出更易维护的 M 脚本。'],
    ],
  },
  analysis:{
    title:'数据分析助手',icon:'chart',historyIcon:'chart',
    headline:'想了解哪些数据变化？',
    subtitle:'写下要分析的指标、时间范围，以及想比较的维度。',
    placeholder:'描述要分析的指标、时间范围和比较维度，例如：按月分析采购金额变化及主要物料贡献…',
    cards:[
      ['layers','数据概览','了解数据范围与核心指标','请根据当前模型的已有核心指标做数据概览，先检查实际数据覆盖范围，再查询实际数值。'],
      ['chart','趋势分析','查看时间走势，识别异常变化','请分析以下指标的时间趋势和异常变化，先确认指标与时间范围：'],
      ['target','贡献分析','比较不同分组的实际表现','请分析以下指标在不同维度上的分布和主要贡献，先确认指标和分组维度：'],
    ],
  },
};

const englishModes = new Map();

export function modeFor(key, locale = language) {
  const mode = MODES[key] || MODES.dax;
  if (locale !== 'en') return mode;
  if (!englishModes.has(mode)) {
    const message = source => translate(locale, source.trimEnd()) + source.slice(source.trimEnd().length);
    englishModes.set(mode, {
      ...mode,
      title: message(mode.title),
      headline: message(mode.headline),
      subtitle: message(mode.subtitle),
      placeholder: message(mode.placeholder),
      cards: mode.cards.map(([symbol, ...copy]) => [symbol, ...copy.map(message)]),
    });
  }
  return englishModes.get(mode);
}
