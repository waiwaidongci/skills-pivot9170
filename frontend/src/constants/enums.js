export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const ARCHIVE = {
  pending:'待核对', reconciling:'人数核对中', switched:'已切换·待清除',
  purging:'等待任务结束', complete:'归档完成', failed:'归档失败',
};
