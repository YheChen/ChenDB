/**
 * The Execution workspace: editor, plan, results, step controls.
 *
 *   ┌──────────────────────┬───────────────────────────────────┐
 *   │  SQL editor          │  Step controls                    │
 *   │                      │  Plan tree  (active op highlit)   │
 *   │                      ├───────────────────────────────────┤
 *   │                      │  Results                          │
 *   └──────────────────────┴───────────────────────────────────┘
 *
 * One editor drives two modes. ⌘↵ runs the statement normally and shows the plan
 * with its final statistics. "Start stepping" runs the same statement on an
 * engine thread that genuinely blocks between operations, and the plan tree then
 * highlights whichever operator the engine is sitting in.
 */

import { useCallback, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { SplitPane } from "@/components/SplitPane";
import { Panel } from "@/components/primitives";
import { useFeature, useRunQuery } from "@/hooks/useEngine";
import { api, type ResumeModeName } from "@/lib/api";
import { EXECUTION_INITIAL_SQL } from "@/lib/demoSql";
import { SqlEditor } from "@/features/sql/SqlEditor";
import type { ExecutionDetail, QueryResultModel } from "@/types/api";
import { AlternativesPanel, PlanTree } from "./PlanTree";
import { ResultsPanel } from "./ResultsPanel";
import { StepControls } from "./StepControls";

const STORAGE_KEY = "chendb.query";

export function ExecutionWorkspace({
  databaseId,
  theme,
  onSelectPage,
}: {
  databaseId: string;
  theme: "light" | "dark";
  onSelectPage?: (pageId: number) => void;
}) {
  const [sql, setSql] = useState<string>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) ?? EXECUTION_INITIAL_SQL;
    } catch {
      return EXECUTION_INITIAL_SQL;
    }
  });
  const [results, setResults] = useState<QueryResultModel[] | undefined>();
  const [execution, setExecution] = useState<ExecutionDetail | null>(null);

  const run = useRunQuery(databaseId);
  const stepAvailable = useFeature("execution_stepping");

  const start = useMutation({
    mutationFn: (statement: string) => api.startSteppedQuery(databaseId, statement),
    onSuccess: setExecution,
  });
  const resume = useMutation({
    mutationFn: ({ id, mode }: { id: string; mode: ResumeModeName }) =>
      mode === "step"
        ? api.stepExecution(id)
        : mode === "continue"
          ? api.continueExecution(id)
          : api.resumeExecution(id, mode),
    onSuccess: setExecution,
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelExecution(id),
    onSuccess: setExecution,
  });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, sql);
    } catch {
      // Storage may be unavailable; the editor still works this session.
    }
  }, [sql]);

  // Switching database invalidates any execution: it belongs to the old one.
  useEffect(() => {
    setExecution(null);
    setResults(undefined);
  }, [databaseId]);

  const onRun = useCallback(() => {
    run.mutate({ sql }, { onSuccess: setResults });
  }, [run, sql]);

  // Step mode runs exactly one statement. Send the last one in the editor, which
  // is the one someone iterating on a query is working on.
  const lastStatement = useCallback((): string => {
    const statements = sql
      .split(";")
      .map((part) => part.trim())
      .filter((part) => part && !part.split("\n").every((l) => l.trim().startsWith("--")));
    return statements.at(-1) ?? sql;
  }, [sql]);

  const stepping = start.isPending || resume.isPending || cancel.isPending;
  const activePlan = execution?.plan ?? results?.at(-1)?.plan ?? null;

  return (
    <SplitPane
      direction="horizontal"
      initialPercent={44}
      minPercent={25}
      maxPercent={70}
      label="Resize the editor against the plan and results"
      className="min-h-0 w-full"
      first={
        <div className="min-h-0 w-full pr-1">
          <SqlEditor
            sql={sql}
            onChange={setSql}
            onParse={onRun}
            result={undefined}
            isPending={run.isPending}
            theme={theme}
            highlight={null}
            onCursorOffset={() => undefined}
            runLabel="Run ⌘↵"
            title="SQL"
          />
        </div>
      }
      second={
        <div className="min-h-0 w-full pl-1">
          <SplitPane
            direction="vertical"
            initialPercent={52}
            minPercent={25}
            maxPercent={75}
            label="Resize the plan against the results"
            className="min-h-0 w-full"
            first={
              <div className="min-h-0 w-full pb-1">
                <Panel
                  title="Execution"
                  subtitle={
                    execution
                      ? `${execution.statement_kind.replace("Statement", "")} · ${execution.state}`
                      : activePlan?.estimated_cost != null
                        ? `volcano operator tree · est. cost ${activePlan.estimated_cost.toFixed(1)}`
                        : "volcano operator tree"
                  }
                  className="h-full"
                  bodyClassName="flex flex-col"
                >
                  <StepControls
                    available={stepAvailable}
                    execution={execution}
                    isPending={stepping}
                    canStart={Boolean(lastStatement())}
                    onStart={() => start.mutate(lastStatement())}
                    onResume={(mode) =>
                      execution &&
                      resume.mutate({ id: execution.execution_id, mode })
                    }
                    onCancel={() =>
                      execution && cancel.mutate(execution.execution_id)
                    }
                  />
                  <div className="scroll-thin min-h-0 flex-1 overflow-auto">
                    <PlanTree
                      plan={activePlan}
                      activeOperatorId={execution?.pause_operator_id ?? null}
                    />
                    {activePlan && activePlan.alternatives.length > 0 ? (
                      <div className="mt-2 border-t border-[var(--border-subtle)]">
                        <p className="text-muted px-3 pt-2 text-[10px] tracking-wide uppercase">
                          what the planner considered
                        </p>
                        <AlternativesPanel plan={activePlan} />
                      </div>
                    ) : null}
                  </div>
                </Panel>
              </div>
            }
            second={
              <div className="min-h-0 w-full pt-1">
                <ResultsPanel
                  results={
                    execution?.result ? [execution.result] : results
                  }
                  isPending={run.isPending}
                  error={run.error}
                  onSelectPage={onSelectPage}
                />
              </div>
            }
          />
        </div>
      }
    />
  );
}
