import type { Message } from './minimal.ts';

// Authored synthetic corpus. Labels are fixed before either implementation runs.
export const evidence: Message[] = [
  { id: 'tea-old', text: '合成用户小林：2021年最喜欢的饮料是乌龙茶。请记住“茶”这个字。', timestamp: '2021-02-03T00:00:00Z' },
  { id: 'budget', text: '合成项目鸢尾的预算是37.50元，票号为A-2048；原文数字不能改写。', timestamp: '2021-07-04T00:00:00Z' },
  { id: 'place', text: '合成用户阿岚的旧书寄存在白鹭书屋，取件口令是虚构名称“蓝松果”。', timestamp: '2022-04-05T00:00:00Z' },
  { id: 'cat', text: '合成故事里的猫叫阿墨，毛色是黑白相间。故事设定不代表现实宠物。', timestamp: '2022-06-07T00:00:00Z' },
  { id: 'allergy', text: '合成用户小林明确说：我不吃花生；这不是说我喜欢花生。', timestamp: '2023-02-04T00:00:00Z' },
  { id: 'future', text: '合成计划：打算2027年去北极看极光，尚未出发。', timestamp: '2023-09-10T00:00:00Z' },
  { id: 'promise', text: '合成约定：每年五月第一周一起整理旧照片，不能被近期闲聊挤掉。', timestamp: '2024-03-08T00:00:00Z' },
  { id: 'project', text: '合成项目代号鸢尾，交付文件名是iris-final-03.zip。', timestamp: '2024-07-08T00:00:00Z' },
  { id: 'quoted', text: '合成引文：故事角色说“我会飞”。这是第三方故事引用，不是用户能力。', timestamp: '2024-10-01T00:00:00Z' },
  { id: 'correction-old', text: '合成陈述：展览日最初记成六月八日。', timestamp: '2025-01-01T00:00:00Z' },
  { id: 'correction-new', text: '合成纠错：展览日不是六月八日，正确的是六月十八日；此前写错了。', timestamp: '2025-01-02T00:00:00Z' },
  { id: 'tea-new', text: '合成现实变化：从2026年十月起，小林最喜欢的饮料改成黑咖啡；2021年的偏好仍是乌龙茶。', timestamp: '2026-10-20T00:00:00Z' },
  { id: 'important-raw', text: '合成重要交代：金额 ¥37.50；编号 A-2048。\n保留换行、全角标点和emoji🙂。', timestamp: '2022-01-01T00:00:00Z' },
  { id: 'long-raw', text: '合成附录。'.repeat(850) + '\n重要尾注：凭单 Z-8192，保留原文🙂。', timestamp: '2022-03-01T00:00:00Z' },
];
export const corpus: Message[] = [...evidence, ...Array.from({ length: 80 }, (_, index) => ({
  id: `recent-noise-${index}`, text: `合成近期无关闲聊第${index}条：今天整理椅子和灯架，记录例行清洁。`,
  timestamp: `2026-10-${String(23 + index % 6).padStart(2, '0')}T12:00:00Z`,
}))];
export interface Query { id: string; group: string; text: string; expected: string[] }
export const queries: Query[] = [
  { id: 'q01', group: 'short', text: '茶', expected: ['tea-old'] },
  { id: 'q02', group: 'short', text: '阿墨', expected: ['cat'] },
  { id: 'q03', group: 'short', text: '鸢尾', expected: ['budget', 'project'] },
  { id: 'q04', group: 'short', text: '书屋', expected: ['place'] },
  { id: 'q05', group: 'number', text: 'A-2048', expected: ['budget', 'important-raw'] },
  { id: 'q06', group: 'number', text: '37.50', expected: ['budget', 'important-raw'] },
  { id: 'q07', group: 'number', text: 'Z-8192', expected: ['long-raw'] },
  { id: 'q08', group: 'number', text: 'iris-final-03.zip', expected: ['project'] },
  { id: 'q09', group: 'historical', text: '2021年小林最喜欢喝什么？', expected: ['tea-old'] },
  { id: 'q10', group: 'historical', text: '几年前说好的整理旧照片是什么时候？', expected: ['promise'] },
  { id: 'q11', group: 'historical', text: '鸢尾项目以前的经费是多少？', expected: ['budget'] },
  { id: 'q12', group: 'semantic', text: '旧书放在哪里，怎样领回来？', expected: ['place'] },
  { id: 'q13', group: 'semantic', text: '那只故事中的小动物叫什么？', expected: ['cat'] },
  { id: 'q14', group: 'semantic', text: '以前共同许下的年度约定', expected: ['promise'] },
  { id: 'q15', group: 'temporal', text: '小林现在最喜欢的饮料是什么？', expected: ['tea-new'] },
  { id: 'q16', group: 'temporal', text: '展览正确的日期是哪天？', expected: ['correction-new'] },
  { id: 'q17', group: 'boundary', text: '小林明确不吃哪种食物？', expected: ['allergy'] },
  { id: 'q18', group: 'boundary', text: '还没有发生的北极旅行计划', expected: ['future'] },
  { id: 'q19', group: 'boundary', text: '故事角色说会飞的那句引用', expected: ['quoted'] },
  { id: 'q20', group: 'recent', text: '第79条近期闲聊整理了什么？', expected: ['recent-noise-79'] },
  { id: 'q21', group: 'exact', text: '保留原话中的换行、符号和表情', expected: ['important-raw'] },
  { id: 'q22', group: 'no-answer', text: '合成用户的银行卡余额是多少？', expected: [] },
  { id: 'q23', group: 'no-answer', text: '从未提过的火星基地地址', expected: [] },
  { id: 'q24', group: 'no-answer', text: '用户养的现实宠物猫的姓名', expected: [] },
];
