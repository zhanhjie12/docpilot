import type { TaskSummary } from "../../types";

// 后端保留稳定英文枚举，页面在这里集中转换为中文标签。

const statusLabel: Record<TaskSummary["status"], string> = { queued: "等待执行", running: "执行中", waiting_human: "等待人工确认", succeeded: "已完成", failed: "执行失败", cancelled: "已取消" };
const taskLabel: Record<string, string> = { parse_and_index: "文档解析与索引", screen: "合同筛选", review: "风险审阅" };
const nodeLabel: Record<string, string> = { queued: "等待调度", parsing: "解析文档", parse: "解析文档", persist: "保存内容", chunking: "文档分块", embedding: "生成向量", indexing: "建立索引", rules: "执行规则", verification: "验证结果", completed: "处理完成" };

export function WorkflowTimeline({ task }: { task: TaskSummary | null }) {
  // 没有任务时保留占位区域，避免详情页布局跳动。
  if (!task) return <div className="timeline muted">当前没有运行中的任务</div>;
  const failed = task.status === "failed";
  return <div className={`timeline ${failed ? "failed" : ""}`}>
    <div className="timeline-bar"><span style={{ width: `${Math.max(0, Math.min(100, task.progress))}%` }} /></div>
    <div className="timeline-meta"><strong>{statusLabel[task.status]}</strong><span>{task.progress}%</span></div>
    <small>{nodeLabel[task.current_node ?? "queued"] ?? "正在处理"} · {taskLabel[task.task_type] ?? "后台任务"}</small>
    {task.failure && <div className="task-failure" role="alert"><strong>{task.failure.code}</strong><span>{task.failure.message}</span></div>}
  </div>;
}
