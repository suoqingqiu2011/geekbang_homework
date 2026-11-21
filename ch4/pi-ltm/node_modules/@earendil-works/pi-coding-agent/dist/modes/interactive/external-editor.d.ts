export interface ExternalEditorOptions {
    command: string;
    content: string;
}
export type ExternalEditorResult = {
    status: "complete";
    content: string;
} | {
    status: "failed";
    message: string;
};
export declare function editInExternalEditor(options: ExternalEditorOptions): Promise<ExternalEditorResult>;
//# sourceMappingURL=external-editor.d.ts.map