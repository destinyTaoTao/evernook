// Minimal MIT upstream snippets pinned to dsh 0.2.1-rc.5 artifacts.
export const publicationPatch = [
  {
    "package": "@deepseek-ai/dsh-tools",
    "file": "lib/index.js",
    "before": "1e53932e7c4213ea24a7d87f06960b7a7702cb21f8a5741c97242b4a470a442a",
    "after": "7f991f40d8f912c24f703e9aaa7bad3edb8662f0fa57d0f9b6e0adbce7f7121a",
    "hunks": [
      [
        "var ToolRuntime = class extends Service {",
        "var ToolRuntime = class extends Service {\n\tstatic evernookPublicationVersion = 1;\n\tevernookPublicationFailures = new Map();\n\tevernookPublicationGuards = new WeakMap();"
      ],
      [
        "\tasync prepareExecution(input, next) {",
        "\tasync prepareExecution(input, next) {\n\t\tconst publicationFailure = this.evernookPublicationFailures.get(input.agent ? String(input.agent.session.id) : null);\n\t\tif (publicationFailure) throw publicationFailure;"
      ],
      [
        "\t\t\tconst returned = await tool.execute(exec.arguments, exec);",
        "\t\t\tconst returned = await tool.execute(exec.arguments, exec);\n\t\t\tconst publicationFailure = this.evernookPublicationFailures.get(exec.agent ? String(exec.agent.session.id) : null);\n\t\t\tif (publicationFailure) throw publicationFailure;"
      ],
      [
        "\tfinishScheduledExecution(exec, result) {",
        "\tasync finishScheduledExecution(exec, result) {\n\t\tconst publicationFailure = this.evernookPublicationFailures.get(exec.agent ? String(exec.agent.session.id) : null);\n\t\tif (publicationFailure) throw publicationFailure;"
      ],
      [
        "\t\tthis.notifyResult(exec, finalResult);\n\t\treturn finalResult;",
        "\t\tawait this.evernookBeforePublish(exec, finalResult);\n\t\tthis.notifyResult(exec, finalResult);\n\t\treturn finalResult;"
      ],
      [
        "function toolErrorResult(error) {",
        "function evernookPublicationError() {\n\tconst error = new Error(\"evernook:tool_publication_failed\");\n\terror.code = \"EVERNOOK_TOOL_PUBLICATION_FAILED\";\n\tObject.defineProperty(error, Symbol.for(\"evernook.tool-publication-failure.v1\"), { value: true });\n\treturn error;\n}\nfunction toolErrorResult(error) {\n\tif (error?.[Symbol.for(\"evernook.tool-publication-failure.v1\")]) throw error;"
      ],
      [
        " : scheduler.finish(parked.exec, parked.result);",
        " : await scheduler.finish(parked.exec, parked.result);"
      ],
      [
        "wakeup();\n\t\t\t\t\t\tdrive();\n\t\t\t\t\t});",
        "wakeup();\n\t\t\t\t\t\tdrive().catch((error) => { runController.abort(error); reject(error); });\n\t\t\t\t\t});"
      ],
      [
        "\t\tconst created = this.createExecution(input);",
        "\t\tconst created = this.createExecution(input);\n\t\tthis.evernookPublicationGuards.set(created.exec, this.ctx.events.dispatch(\"emit\", [scopeTarget(this, created.exec.agent), \"evernook/tools/before-publish\", created.exec]));"
      ],
      [
        "\t/** Apply the snapshotted tool-owned content transform without exposing other result fields. */",
        "\tasync evernookBeforePublish(exec, result, phase = \"result\") {\n\t\tconst key = exec.agent ? String(exec.agent.session.id) : null;\n\t\tconst prior = this.evernookPublicationFailures.get(key);\n\t\tif (prior) throw prior;\n\t\tconst snapshot = phase === \"ptc-call\" ? Object.freeze({ ...exec }) : Object.freeze(exec);\n\t\ttry {\n\t\t\tfor (const callback of this.evernookPublicationGuards.get(exec) ?? []) await callback(snapshot, result, phase);\n\t\t} catch {\n\t\t\tconst error = evernookPublicationError();\n\t\t\tthis.evernookPublicationFailures.set(key, error);\n\t\t\tthrow error;\n\t\t}\n\t}\n\t/** Apply the snapshotted tool-owned content transform without exposing other result fields. */"
      ],
      [
        "\t\t\t\t\t\tconst settle = (result) => {\n\t\t\t\t\t\t\tresolve(result.isError ? {\n\t\t\t\t\t\t\t\tisError: true,\n\t\t\t\t\t\t\t\tmessage: result.error.message\n\t\t\t\t\t\t\t} : {\n\t\t\t\t\t\t\t\tisError: false,\n\t\t\t\t\t\t\t\tvalue: result.value\n\t\t\t\t\t\t\t});\n\t\t\t\t\t\t\tconst agent = exec.agent;\n\t\t\t\t\t\t\tif (agent === void 0) return;\n\t\t\t\t\t\t\tconst task = (async () => {\n\t\t\t\t\t\t\t\tconst logged = await shapeDispatchLog({\n\t\t\t\t\t\t\t\t\texec,\n\t\t\t\t\t\t\t\t\tagent,\n\t\t\t\t\t\t\t\t\tsubCallId,\n\t\t\t\t\t\t\t\t\tname,\n\t\t\t\t\t\t\t\t\tisError: result.isError,\n\t\t\t\t\t\t\t\t\tcontent: result.content\n\t\t\t\t\t\t\t\t});\n\t\t\t\t\t\t\t\tagent.session.append(\"tool/ptc-dispatch\", {\n\t\t\t\t\t\t\t\t\trootCallId: exec.rootCallId,\n\t\t\t\t\t\t\t\t\tparentCallId: exec.callId,\n\t\t\t\t\t\t\t\t\tsubCallId,\n\t\t\t\t\t\t\t\t\tname,\n\t\t\t\t\t\t\t\t\targuments: normalized.logged,\n\t\t\t\t\t\t\t\t\tisError: result.isError,\n\t\t\t\t\t\t\t\t\t...result.error?.info === void 0 ? {} : { error: result.error.info },\n\t\t\t\t\t\t\t\t\tcontent: logged,\n\t\t\t\t\t\t\t\t\t...result.meta === void 0 ? {} : { meta: result.meta }\n\t\t\t\t\t\t\t\t});\n\t\t\t\t\t\t\t})().finally(() => {\n\t\t\t\t\t\t\t\tlogWork.delete(task);\n\t\t\t\t\t\t\t});\n\t\t\t\t\t\t\tlogWork.add(task);\n\t\t\t\t\t\t};\n",
        "\t\t\t\t\t\tconst settle = async (result) => {\n\t\t\t\t\t\t\tconst agent = exec.agent;\n\t\t\t\t\t\t\tif (agent !== void 0) {\n\t\t\t\t\t\t\t\tconst logged = await shapeDispatchLog({ exec, agent, subCallId, name, isError: result.isError, content: result.content });\n\t\t\t\t\t\t\t\tconst data = { rootCallId: exec.rootCallId, parentCallId: exec.callId, subCallId, name, arguments: normalized.logged,\n\t\t\t\t\t\t\t\t\tisError: result.isError, ...result.error?.info === void 0 ? {} : { error: result.error.info },\n\t\t\t\t\t\t\t\t\tcontent: logged, ...result.meta === void 0 ? {} : { meta: result.meta } };\n\t\t\t\t\t\t\t\tawait registry.evernookBeforePublish(parked.exec, registry.materializeFinalResult({ isError: false, value: data, content: logged }), \"ptc-log\");\n\t\t\t\t\t\t\t\tagent.session.append(\"tool/ptc-dispatch\", data);\n\t\t\t\t\t\t\t}\n\t\t\t\t\t\t\tresolve(result.isError ? { isError: true, message: result.error.message } : { isError: false, value: result.value });\n\t\t\t\t\t\t};\n"
      ],
      [
        "\t\t\t\t\t\t\t\tsettle(result);",
        "\t\t\t\t\t\t\t\tawait settle(result);"
      ],
      [
        "\t\t\t\t\t\t\t\texec.agent?.session.append(\"tool/ptc-dispatch-start\", {\n\t\t\t\t\t\t\t\t\trootCallId: exec.rootCallId,\n\t\t\t\t\t\t\t\t\tparentCallId: exec.callId,\n\t\t\t\t\t\t\t\t\tsubCallId,\n\t\t\t\t\t\t\t\t\tname,\n\t\t\t\t\t\t\t\t\targuments: normalized.logged\n\t\t\t\t\t\t\t\t});\n\t\t\t\t\t\t\t\tconst prepared = await scheduler.prepare(input);",
        "\t\t\t\t\t\t\t\tconst prepared = await scheduler.prepare(input);\n\t\t\t\t\t\t\t\tconst data = { rootCallId: exec.rootCallId, parentCallId: exec.callId, subCallId, name, arguments: normalized.logged };\n\t\t\t\t\t\t\t\tawait registry.evernookBeforePublish(prepared.exec, registry.materializeFinalResult({ isError: false, value: data, content: [] }), \"ptc-call\");\n\t\t\t\t\t\t\t\texec.agent?.session.append(\"tool/ptc-dispatch-start\", data);"
      ],
      [
        "\t\t\t\tlet driverRun = Promise.resolve();",
        "\t\t\t\tlet driverRun = Promise.resolve();\n\t\t\t\tlet driverFailure;"
      ],
      [
        "\t\t\t\tconst drive = () => {\n\t\t\t\t\tif (driving)",
        "\t\t\t\tconst drive = () => {\n\t\t\t\t\tif (driverFailure) return Promise.reject(driverFailure);\n\t\t\t\t\tif (driving)"
      ],
      [
        "\t\t\t\t\t\t} finally {\n\t\t\t\t\t\t\tdriving = false;",
        "\t\t\t\t\t\t} catch (error) {\n\t\t\t\t\t\t\tdriverFailure = error;\n\t\t\t\t\t\t\trunController.abort(error);\n\t\t\t\t\t\t\tawait Promise.allSettled([...inFlight]);\n\t\t\t\t\t\t\tthrow error;\n\t\t\t\t\t\t} finally {\n\t\t\t\t\t\t\tdriving = false;"
      ]
    ]
  },
  {
    "package": "@deepseek-ai/dsh-agent-loop",
    "file": "lib/index.js",
    "before": "cf57e3ecd487ac5c2553455aaa5cef2c6fb41e6bd61be5aa455675d1d7193033",
    "after": "c340f734fe36fcf06144fbf4ea159fad01e82a0f8ce477b41b97d5c6a3c38b40",
    "hunks": [
      [
        "async function executeToolCalls(ctx, turn, step, toolCalls, signal, acceptContext) {",
        "function evernookLoopPublicationError() {\n\tconst error = new Error(\"evernook:tool_publication_failed\");\n\terror.code = \"EVERNOOK_TOOL_PUBLICATION_FAILED\";\n\tObject.defineProperty(error, Symbol.for(\"evernook.tool-publication-failure.v1\"), { value: true });\n\treturn error;\n}\nfunction evernookSessionBlocked(events) {\n\tconst recovery = new ToolCallRecovery();\n\tfor (const event of events) recovery.observe(event);\n\treturn recovery.results().length > 0 || events.some((event) => event.type === \"plugin:dsh-evernook/publication-blocked\");\n}\nasync function executeToolCalls(ctx, turn, step, toolCalls, signal, acceptContext) {"
      ],
      [
        " : ctx.tools[TOOL_RUNTIME_SCHEDULER].finish(slot.exec, slot.result);",
        " : await ctx.tools[TOOL_RUNTIME_SCHEDULER].finish(slot.exec, slot.result);"
      ],
      [
        "\tasync turn() {",
        "\tasync turn() {\n\t\tif (evernookSessionBlocked(this.session.snapshotEvents())) this.throwError(evernookLoopPublicationError());"
      ],
      [
        "\t\t\t\t} catch (error) {\n\t\t\t\t\ttry {\n\t\t\t\t\t\tfor (const event of toolRecovery.results())",
        "\t\t\t\t} catch (error) {\n\t\t\t\t\tif (error?.[Symbol.for(\"evernook.tool-publication-failure.v1\")]) {\n\t\t\t\t\t\tpublicationBlocked = true;\n\t\t\t\t\t\ttry {\n\t\t\t\t\t\t\tappendPluginRecord(this.session, \"plugin:dsh-evernook/publication-blocked\", { version: 1, code: \"EVERNOOK_TOOL_PUBLICATION_FAILED\", turn, step });\n\t\t\t\t\t\t\tawait this.loopCtx.sessions.flush(this.session);\n\t\t\t\t\t\t} catch {}\n\t\t\t\t\t\tthrow error;\n\t\t\t\t\t}\n\t\t\t\t\ttry {\n\t\t\t\t\t\tfor (const event of toolRecovery.results())"
      ],
      [
        "\t\t\t\t\tconst closers = interruptedTurnClosers(persisted);",
        "\t\t\t\t\tif (evernookSessionBlocked(persisted)) throw evernookLoopPublicationError();\n\t\t\t\t\tconst closers = interruptedTurnClosers(persisted);"
      ],
      [
        "appendSkippedToolCall(session, turn, step, call.block);",
        "await appendSkippedToolCall(ctx, turn, step, call.block);",
        2
      ],
      [
        "function appendSkippedToolCall(session, turn, step, block) {\n\tconst callSeq = appendToolCall(session, turn, step, block);\n\tappendToolResult(session, turn, step, block, {\n\t\tcontent: [{\n\t\t\ttype: \"text\",\n\t\t\ttext: \"Error: tool call aborted before dispatch\"\n\t\t}],\n\t\tisError: true,\n\t\terror: {\n\t\t\tmessage: \"tool call aborted before dispatch\",\n\t\t\tinfo: {\n\t\t\t\tname: \"AbortError\",\n\t\t\t\tcode: TOOL_ABORTED_BEFORE_DISPATCH\n\t\t\t}\n\t\t}\n\t}, callSeq);\n}",
        "async function appendSkippedToolCall(ctx, turn, step, block) {\n\tconst agent = ctx.agents.requireInitiator();\n\tconst { session } = agent;\n\tconst callSeq = appendToolCall(session, turn, step, block);\n\tconst result = await ctx.tools.execute({ callId: block.id, name: block.name, arguments: parseArguments(block.arguments), agent, signal: AbortSignal.abort() });\n\tappendToolResult(session, turn, step, block, result, callSeq);\n}"
      ],
      [
        "import { SessionLogOffset,",
        "import { appendPluginRecord, SessionLogOffset,"
      ],
      [
        "\t\tlet target = \"next-turn\";",
        "\t\tlet target = \"next-turn\";\n\t\tlet publicationBlocked = false;"
      ],
      [
        "\t\t\t\t\tthis.session.append(\"step/end\", {",
        "\t\t\t\t\tif (!publicationBlocked) this.session.append(\"step/end\", {"
      ],
      [
        "\t\t\t\tthis.session.append(\"turn/end\", {",
        "\t\t\t\tif (!publicationBlocked) this.session.append(\"turn/end\", {"
      ]
    ]
  },
  {
    "package": "@deepseek-ai/dsh-tools",
    "file": "lib/types/index.d.ts",
    "before": "049df0624956583b8ca05badfd6e9115c6933481571dc17965f36f407bdf95f0",
    "after": "0771291d38cf7252920c51225956cbff485d186c37df880af67dd8eb9bc77831",
    "hunks": [
      [
        "finish(exec: ToolRunContext, result: ToolExecutionResult): ToolExecutionResult;",
        "finish(exec: ToolRunContext, result: ToolExecutionResult): Promise<ToolExecutionResult>;"
      ]
    ]
  }
];

export function transformPublicationSource(source, hunks) {
  for (const [before, after, count = 1] of hunks) {
    if (source.split(before).length !== count + 1) throw new Error('Publication patch anchor count does not match');
    source = source.split(before).join(after);
  }
  return source;
}
