// Use the bundled public artifact-tool entry point; no repository npm install.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const [runFilename, moduleRoot, ...options] = process.argv.slice(2);
if (!runFilename || !moduleRoot) throw Error('Expected run.json and bundled node_modules paths');
const require = createRequire(path.join(path.dirname(path.resolve(moduleRoot)), 'package.json'));
const {Workbook, SpreadsheetFile, FileBlob} = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const data = JSON.parse(await fs.readFile(runFilename, 'utf8'));
const directory = path.dirname(path.resolve(runFilename));
const filename = path.join(directory, '话术对比.xlsx');

if (options.includes('--preview')) {
  const book = await SpreadsheetFile.importXlsx(await FileBlob.load(filename));
  for (const [sheetName, range, name] of [['话术对比', 'F5:I7', '对比预览.png'], ['原始聊天', 'A5:G9', '聊天预览.png'], ['提示词与参数', 'A5:B9', '参数预览.png']]) {
    const png = await book.render({sheetName, range, scale: 1, format: 'png'});
    await fs.writeFile(path.join(directory, name), new Uint8Array(await png.arrayBuffer()));
  }
  process.exit(0);
}

const records = data.records;
if (!records?.length) throw Error('No selected records');
const count = records.length;
const last = count + 5;
const book = Workbook.create();
const main = book.worksheets.add('话术对比');
const chat = book.worksheets.add('原始聊天');
const params = book.worksheets.add('提示词与参数');
const literal = value => typeof value === 'string' && /^[=+@-]/.test(value) ? "'" + value : value;
const lengths = s => Math.max(1, String(s || '').split('\n').reduce((n, line) => n + Math.max(1, Math.ceil(line.length / 27)), 0));
const labels = {success: '模型生成成功', fallback: '缺聊天固定兜底', error: '生成失败'};

function format(sheet, bottom, col, widths, frozen=3) {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${col}${bottom}`).format.font = {name: 'Microsoft YaHei', size: 11, color: '#243247'};
  sheet.getRange(`A1:${col}${bottom}`).format.verticalAlignment = 'top';
  sheet.getRange('A2').format.font = {name: 'Microsoft YaHei', size: 16, color: '#192E48', bold: true};
  sheet.getRange(`A2:${col}2`).format.rowHeight = 32;
  sheet.getRange(`A3:${col}4`).format.rowHeight = 25;
  sheet.getRange(`A3:${col}4`).format.font.color = '#566477';
  sheet.getRange(`A5:${col}5`).format = {fill: '#253C59', font: {name: 'Microsoft YaHei', size: 11, color: '#FFFFFF', bold: true}, wrapText: true, horizontalAlignment: 'center', verticalAlignment: 'center', rowHeight: 34};
  sheet.freezePanes.freezeRows(5);
  if (frozen) sheet.freezePanes.freezeColumns(frozen);
  widths.forEach((w, i) => sheet.getRangeByIndexes(0, i, bottom, 1).format.columnWidth = w);
}

format(main, last, 'Q', [8, 20, 28, 26, 36, 60, 60, 21, 50, 11, 11, 18, 38, 11, 12, 35, 30]);
main.tabColor = '#253C59';
const modes = records.reduce((result, r) => (result[r.result?.status || 'pending'] = (result[r.result?.status || 'pending'] || 0) + 1, result), {});
main.getRange('A2').values = [[`${data.title}提示词测试：新旧话术对比`]];
main.getRange('A3').values = [[`目标${data.requested_count}单，入选${count}单；成功${modes.success || 0}，兜底${modes.fallback || 0}，失败${modes.error || 0}，未生成${modes.pending || 0}。测试时间：${data.created_at}`]];
main.getRange('A4').values = [['历史文字为本地保存值，本次输出未人工改写；原分类和已选原因不代表核验事实，自动提示不代替证据审查。']];
main.getRange('A5:Q5').values = [['序号', '店铺', '订单号', '原分类／申诉项', '已保存的实际原因', '历史话术（本地保存）', '本次正式函数输出', '本次生成状态', '自动核对提示', '历史字数', '本次字数', '历史记录状态', '聊天来源及归属', 'API次数', '耗时/秒', '商品', '历史记录标识']];
const rows = records.map((r, i) => [i+1, r.shop, r.order_no, data.kind === 'negative' ? r.classification : r.appeal_item || '负向体验复议', r.reason, r.history_text, r.result?.text || '', labels[r.result?.status] || '尚未生成', (r.notes || []).join('\n'), null, null, r.history_status, `${r.chat_source}；${r.association}`, r.result?.calls?.length || 0, r.result?.seconds ?? null, r.goods_title, r.record_key].map(literal));
main.getRange(`A6:Q${last}`).values = rows;
main.getRange(`C6:C${last}`).setNumberFormat('@');
main.getRange(`O6:O${last}`).setNumberFormat('0.00');
main.getRange(`D6:I${last}`).format.wrapText = true;
main.getRange(`L6:M${last}`).format.wrapText = true;
main.getRange(`P6:Q${last}`).format.wrapText = true;
main.getRange(`J6:K${last}`).formulas = records.map((r, i) => [`=LEN(F${i+6})`, `=LEN(G${i+6})`]);
rows.forEach((r, i) => {
  const lines = Math.max(lengths(r[5]), lengths(r[6]), String(r[8]).split('\n').reduce((n,s)=>n+Math.max(1,Math.ceil(s.length/23)),0), Math.ceil(String(r[4]).length/16), Math.ceil(String(r[12]).length/17), Math.ceil(String(r[15]).length/16));
  main.getRange(`A${i+6}:Q${i+6}`).format.rowHeight = Math.max(80, Math.min(409, lines*18+12));
  if (i%2 === 0) main.getRange(`A${i+6}:Q${i+6}`).format.fill = '#F2F5F8';
  main.getRange(`G${i+6}`).format.fill = i%2 === 0 ? '#E8EFF8' : '#F2F6FB';
});
main.tables.add(`A5:Q${last}`, true, 'PromptComparison').showFilterButton = true;
main.getRange(`H6:H${last}`).conditionalFormats.add('containsText', {text: '失败', format: {fill: '#FCE5E5', font: {color: '#9F2A2A', bold: true}}});
main.getRange(`H6:H${last}`).conditionalFormats.add('containsText', {text: '兜底', format: {fill: '#FFF0DB', font: {color: '#8A4F0E'}}});
main.getRange(`I6:I${last}`).conditionalFormats.add('containsText', {text: '截断', format: {fill: '#FFF0DB', font: {color: '#8A4F0E'}}});

const messages = [];
records.forEach((r,i) => r.messages.forEach((m,j) => messages.push([i+1, r.shop, r.order_no, j+1, m.is_system_message ? '系统' : m.sender.includes('*') ? '买家' : '客服', m.time || '', m.content || (m.image_urls.length ? '[图片]' : ''), m.image_urls.length, r.chat_source].map(literal))));
const chatLast = Math.max(6, messages.length+5);
format(chat, chatLast, 'I', [10, 20, 28, 11, 10, 25, 105, 12, 30]);
chat.getRange('A2').values = [['原始聊天明细']];
chat.getRange('A3').values = [[`${count}个订单，共${messages.length}条原始消息；缺聊天订单不补写消息。`]];
chat.getRange('A4').values = [['来源：本地同店铺、同订单的历史记录；保留原时间与内容，图片和视频未被本测试识别。']];
chat.getRange('A5:I5').values = [['订单序号','店铺','订单号','消息序号','角色','原始时间','原始内容','图片数量','聊天来源']];
if (messages.length) {
  chat.getRange(`A6:I${chatLast}`).values = messages;
  chat.getRange(`C6:C${chatLast}`).setNumberFormat('@');
  chat.getRange(`G6:I${chatLast}`).format.wrapText = true;
  messages.forEach((r,i) => {
    const lines = String(r[6]).split('\n').reduce((n,s) => n+Math.max(1, Math.ceil(s.length/49)), 0);
    chat.getRange(`A${i+6}:I${i+6}`).format.rowHeight = Math.max(28, Math.min(409, lines*18+8));
    if (r[0]%2 === 0) chat.getRange(`A${i+6}:I${i+6}`).format.fill = '#F2F5F8';
  });
  chat.tables.add(`A5:I${chatLast}`, true, 'OriginalMessages').showFilterButton = true;
} else {
  chat.getRange('A6').values = [['本轮所选订单均无聊天，结果为固定兜底。']];
}

const calls = records.flatMap(r => r.result?.calls || []);
const unique = key => [...new Set(calls.map(c=>c[key]).filter(v=>v!==undefined&&v!==null))].join('、') || '无API调用';
const parameterRows = [
  ['测试类型', data.title], ['测试时间', `北京时间 ${data.created_at}`],
  ['提示词来源', data.prompt_file || '当前项目正式提示词'],
  ['提示词SHA256', data.prompt_sha256],
  ['数量', `目标${data.requested_count}单；实际${count}单；成功${modes.success||0}，兜底${modes.fallback||0}，失败${modes.error||0}`],
  ['实际请求模型', unique('model')], ['API返回模型名', unique('response_model')],
  ['temperature实参', unique('temperature')], ['max_tokens实参', unique('max_tokens')],
  ['API请求总数', calls.length],
  ['输入token（接口记录）', calls.reduce((n,c)=>n+(c.usage?.prompt_tokens||0),0)],
  ['输出token（接口记录）', calls.reduce((n,c)=>n+(c.usage?.completion_tokens||0),0)],
  ['筛选统计', JSON.stringify(data.selection, null, 2)],
  ['历史比较口径', '历史文为本地保存字段，不保证等同平台提交时快照；历史状态仅作来源说明，不能据文字推断申诉成败。'],
  ['生成口径', '使用对应正式函数；不重跑分类；实际原因保持原记录，不以历史话术充当事实。自定义提示词只替换本测试进程的system内容。'],
  ['文字处理', '负向函数通常按120字处理，复议200字、售后300字；保留当前正式函数实际行为，原始候选和每次请求另存run.json。'],
  ['失败与兜底', '生成失败单独显示，不复制旧文代替；缺聊天兜底不算模型生成，可在修复后断点续跑。'],
  ['自动提示局限', '仅检查截断、输出上限、媒体标记、多段及归属等线索，不自动认定买家过错、图片有效性或商家免责。'],
  ['运行边界', '只读业务数据库，不打开浏览器、不提交申诉；不写入正式模型、提示词或历史记录。'],
  ['代码快照', JSON.stringify(data.source_hashes, null, 2)],
];
for (const [i, paragraph] of data.effective_prompt.split(/\n\s*\n/).entries()) {
  const characters = Array.from(paragraph);
  for (let j=0;j<characters.length;j+=1000) parameterRows.push([`实际提示词·段${i+1}${j?`续${j/1000}`:''}`, characters.slice(j,j+1000).join('')]);
}
const paramLast = parameterRows.length+5;
format(params, paramLast, 'B', [28, 125], 0);
params.getRange('A2').values = [['本轮提示词与调用参数']];
params.getRange('A3').values = [['实际system内容完整保留；每次请求的附加去重上下文和原始返回见run.json。']];
params.getRange('A4').values = [['同一运行目录支持断点续跑及重新导出；修改提示词后应新建一轮，避免混合结果。']];
params.getRange('A5:B5').values = [['项目','内容']];
params.getRange(`A6:B${paramLast}`).values = parameterRows.map(r=>r.map(literal));
params.getRange(`A6:B${paramLast}`).format.wrapText = true;
parameterRows.forEach((r,i) => {
  const lines = String(r[1]).split('\n').reduce((n,s)=>n+Math.max(1,Math.ceil(s.length/57)),0);
  params.getRange(`A${i+6}:B${i+6}`).format.rowHeight = Math.max(28,Math.min(409,lines*18+10));
  if(i%2===0)params.getRange(`A${i+6}:B${i+6}`).format.fill='#F2F5F8';
});
book.recalculate();
main.getRange(`J6:K${last}`).values.forEach((r,i)=>{
  if(r[0]!==records[i].history_text.length||r[1]!==String(records[i].result?.text||'').length)throw Error('Length formula mismatch');
});
const errors=await book.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:10},maxChars:600});
console.log(errors.ndjson);
await (await SpreadsheetFile.exportXlsx(book)).save(filename);
console.log(JSON.stringify({filename,orders:count,messages:messages.length}));
