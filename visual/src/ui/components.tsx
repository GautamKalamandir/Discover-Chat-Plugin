import * as React from "react";

import { ClarificationOption, TableData } from "../api/types";
import { ChatMessage, stageLabel } from "../chat/store";
import { Block, Inline, parseMarkdown } from "./markdown";

export const MAX_TABLE_ROWS = 200;

// --- markdown (AST -> React elements; text is never interpreted as HTML) -------------------------

function InlineText({ inlines }: { inlines: Inline[] }): React.JSX.Element {
    return (
        <>
            {inlines.map((part, i) =>
                part.kind === "bold" ? (
                    <strong key={i}>{part.text}</strong>
                ) : part.kind === "italic" ? (
                    <em key={i}>{part.text}</em>
                ) : (
                    <React.Fragment key={i}>{part.text}</React.Fragment>
                ),
            )}
        </>
    );
}

export function Markdown({ text }: { text: string }): React.JSX.Element {
    const blocks: Block[] = parseMarkdown(text);
    return (
        <>
            {blocks.map((block, i) =>
                block.kind === "p" ? (
                    <p key={i}>
                        <InlineText inlines={block.inlines} />
                    </p>
                ) : (
                    <ul key={i}>
                        {block.items.map((item, j) => (
                            <li key={j}>
                                <InlineText inlines={item} />
                            </li>
                        ))}
                    </ul>
                ),
            )}
        </>
    );
}

// --- result table ---------------------------------------------------------------------------------

/** "[Total Net Sales]" -> "Total Net Sales", "Product[LOB]" -> "LOB". */
export function columnLabel(column: string): string {
    const match = /\[([^\]]+)\]$/.exec(column);
    return match ? match[1] : column;
}

export function formatCell(value: unknown): string {
    if (value === null || value === undefined) return "";
    if (typeof value === "number") {
        return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
    }
    return String(value);
}

export function ResultTable({ table }: { table: TableData }): React.JSX.Element {
    const rows = table.rows.slice(0, MAX_TABLE_ROWS);
    return (
        <figure className="result">
            <figcaption>{table.title}</figcaption>
            <div className="result-scroll">
                <table>
                    <thead>
                        <tr>
                            {table.columns.map((c) => (
                                <th key={c} scope="col">
                                    {columnLabel(c)}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row, i) => (
                            <tr key={i}>
                                {table.columns.map((c) => (
                                    <td key={c} className={typeof row[c] === "number" ? "num" : ""}>
                                        {formatCell(row[c])}
                                    </td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
            {(table.truncated || table.rows.length > rows.length) && (
                <p className="note">Only part of the result is shown.</p>
            )}
        </figure>
    );
}

// --- messages -------------------------------------------------------------------------------------

/** The answers to a clarification (ADR 0014): one button each, plus "None of these", which hands
 * over to typing. Disabled while answering and once the question is answered. */
export function ClarificationOptions({
    options,
    disabled,
    onChoose,
    onNone,
}: {
    options: ClarificationOption[];
    disabled: boolean;
    onChoose: (option: ClarificationOption) => void;
    onNone: () => void;
}): React.JSX.Element {
    return (
        <div className="options" role="group" aria-label="Choose an answer">
            {options.map((option) => (
                <button
                    key={option.id}
                    type="button"
                    className="option"
                    disabled={disabled}
                    onClick={() => onChoose(option)}
                >
                    {option.label}
                </button>
            ))}
            <button type="button" className="option none" disabled={disabled} onClick={onNone}>
                None of these
            </button>
        </div>
    );
}

export function MessageView({
    message,
    stage,
    busy = false,
    onChoose,
    onNone,
    children,
}: {
    message: ChatMessage;
    stage: string | null;
    busy?: boolean;
    onChoose?: (message: ChatMessage, option: ClarificationOption) => void;
    onNone?: () => void;
    /** Extra content in the bubble (a /summary report). */
    children?: React.ReactNode;
}): React.JSX.Element {
    const progress = message.kind === "report" ? (message.report?.progress ?? null) : stageLabel(stage);
    const label = message.pending ? progress : null;
    const options = message.kind === "clarification" ? (message.options ?? []) : [];
    return (
        <div className={`msg ${message.role} ${message.kind}`} data-testid={`msg-${message.kind}`}>
            {message.role === "user" ? (
                <p>{message.text}</p>
            ) : (
                <>
                    {message.tables.map((t, i) => (
                        <ResultTable key={i} table={t} />
                    ))}
                    {message.text && <Markdown text={message.text} />}
                    {children}
                    {options.length > 0 && (
                        <ClarificationOptions
                            options={options}
                            disabled={busy || !message.open}
                            onChoose={(option) => onChoose?.(message, option)}
                            onNone={() => onNone?.()}
                        />
                    )}
                    {label && (
                        <p className="stage" role="status">
                            {label}
                        </p>
                    )}
                </>
            )}
        </div>
    );
}

export function Composer({
    busy,
    onSend,
    onStop,
    inputRef,
}: {
    busy: boolean;
    onSend: (question: string) => void;
    onStop: () => void;
    inputRef?: React.Ref<HTMLTextAreaElement>;
}): React.JSX.Element {
    const [text, setText] = React.useState("");
    const send = (): void => {
        const question = text.trim();
        if (!question || busy) return;
        onSend(question);
        setText("");
    };
    // Not a <form>: Power BI's sandboxed visual frame blocks form submission, so a submit button
    // would silently do nothing. Send is a plain button calling send() directly.
    return (
        <div className="composer">
            <textarea
                ref={inputRef}
                aria-label="Ask a question about your data"
                placeholder="Ask a question about your data…"
                rows={2}
                maxLength={2000}
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        send();
                    }
                }}
            />
            {busy ? (
                <button type="button" onClick={onStop} aria-label="Stop answering">
                    Stop
                </button>
            ) : (
                <button
                    type="button"
                    onClick={send}
                    disabled={!text.trim()}
                    aria-label="Send question"
                >
                    Send
                </button>
            )}
        </div>
    );
}
