/**
 * Catches render errors so one broken panel cannot blank the whole tool.
 *
 * React has no hook equivalent: error boundaries must be class components.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/primitives";

interface Props {
  children: ReactNode;
  /** Shown in the fallback so the user knows which panel failed. */
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep the stack in the console: this is a developer tool, and whoever is
    // using it can act on a stack trace.
    console.error(`[${this.props.label ?? "panel"}] render failed`, error, info);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        role="alert"
        className="surface flex h-full flex-col items-center justify-center gap-2 rounded-lg border p-6 text-center"
      >
        <p className="text-sm font-medium text-red-600 dark:text-red-400">
          {this.props.label ?? "This panel"} crashed
        </p>
        <p className="text-muted max-w-md font-mono text-[11px]">{error.message}</p>
        <Button onClick={() => this.setState({ error: null })}>Try again</Button>
      </div>
    );
  }
}
