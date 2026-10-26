/**
 * 自动生成 —— 不要手工编辑。
 *
 * 来源: contracts/openapi.json（`python tools/export_openapi.py` 从 FastAPI 导出）
 * 生成: python tools/gen_api_types.py
 *
 * 用法:
 *     import type {{ components }} from "@/api/schema";
 *     export type Conversation = components["schemas"]["ConversationOut"];
 *
 * 已知边界: SSE 流式接口的帧结构不在 OpenAPI 里，见 contracts/README.md。
 */

export interface components {
  schemas: {
    AbstractAnalysisBody: {
      limit?: number | null;
      result_refs?: string[];
    };
    AcConclusionIn: {
      ac_code: string;
      ai_relation?: string;
      conclusion: string;
      reason?: string;
    };
    AcIn: {
      ac_code?: string;
      check_type?: string;
      evidence_requirement?: string;
      required?: boolean;
      statement: string;
    };
    AcOut: {
      ac_code: string;
      check_type: string;
      evidence_requirement: string;
      required: boolean;
      statement: string;
      version: number;
    };
    AddMemberBody: {
      role: string;
      user_id: string;
    };
    AdoptBody: {
      ac_code: string;
      reason?: string;
    };
    AdoptOut: {
      ac_code: string;
      ai_relation: string;
      conclusion: string;
      review_code: string;
      run_code: string;
      verdict: string;
    };
    AnalyzeBody: {
      attachment_ref: string;
      main_file?: string;
    };
    ArchiveDecideBody: {
      approve: boolean;
      reason?: string;
    };
    ArchiveRequestBody: {
      reason?: string;
    };
    AttachBody: {
      submission_ref: string;
    };
    AttachmentOut: {
      attachment_id: string;
      code: string;
      create_time: string | null;
      ext: string;
      kind: string;
      mime_type: string;
      name: string;
      sha256: string;
      size: number;
      status: "active" | "replaced" | "removed";
      submission_id: string | null;
      task_id: string | null;
      uploaded_by: string;
      version: number;
    };
    AuditEventOut: {
      action: string;
      actor_id: string;
      actor_role: string;
      created_at: string | null;
      detail: string;
      event_code: string;
      object_code: string;
      object_type: string;
    };
    Body_call_tool_tools_call_post: {
      name: string;
      tool_input?: {
        [key: string]: unknown;
      };
    };
    Body_chat_feedback_chat_feedback_post: {
      message_id?: string;
      reason?: string;
      score?: number;
    };
    Body_completion_other_completion_post: {
      echo?: boolean;
      max_tokens?: number | null;
      model_name?: string;
      prompt_name?: string;
      query: string;
      stream?: boolean;
      temperature?: number;
    };
    Body_create_kb_knowledge_base_create_knowledge_base_post: {
      embed_model?: string;
      kb_info?: string;
      knowledge_base_name: string;
      vector_store_type?: string;
    };
    Body_delete_docs_knowledge_base_delete_docs_post: {
      delete_content?: boolean;
      file_names: string[];
      knowledge_base_name: string;
      not_refresh_vs_cache?: boolean;
    };
    Body_file_chat_chat_file_chat_post: {
      history?: components["schemas"]["History"][];
      knowledge_id: string;
      max_tokens?: number | null;
      model_name?: string;
      prompt_name?: string;
      query: string;
      score_threshold?: number;
      stream?: boolean;
      temperature?: number;
      top_k?: number;
    };
    Body_files_v1_files_post: {
      file: string;
    };
    Body_get_server_prompt_template_server_get_prompt_template_post: {
      name?: string;
      type?: string;
    };
    Body_kb_chat_chat_kb_chat_post: {
      conversation_id?: string | null;
      history?: components["schemas"]["History"][];
      kb_name?: string;
      max_tokens?: number | null;
      mode?: "local_kb" | "temp_kb" | "search_engine";
      model?: string;
      project_id?: string | null;
      prompt_name?: string;
      query: string;
      return_direct?: boolean;
      score_threshold?: number;
      stream?: boolean;
      temperature?: number;
      temporary?: boolean;
      top_k?: number;
      use_memory?: boolean;
    };
    Body_recreate_summary_vector_store_knowledge_base_kb_summary_api_recreate_summary_vector_store_post: {
      allow_empty_kb?: boolean;
      embed_model?: string;
      file_description?: string;
      knowledge_base_name: string;
      max_tokens?: number | null;
      model_name?: string;
      temperature?: number;
      vs_type?: string;
    };
    Body_recreate_vector_store_knowledge_base_recreate_vector_store_post: {
      allow_empty_kb?: boolean;
      chunk_overlap?: number;
      chunk_size?: number;
      embed_model?: string;
      knowledge_base_name: string;
      not_refresh_vs_cache?: boolean;
      vs_type?: string;
      zh_title_enhance?: boolean;
    };
    Body_resume_agent_run_agent_runs__run_id__resume_post: {
      tool_config?: {
        [key: string]: unknown;
      } | null;
      use_mcp?: boolean;
      use_memory?: boolean;
    };
    Body_search_docs_knowledge_base_search_docs_post: {
      file_name?: string;
      knowledge_base_name: string;
      metadata?: {
        [key: string]: unknown;
      };
      query?: string;
      score_threshold?: number;
      top_k?: number;
    };
    Body_search_temp_docs_knowledge_base_search_temp_docs_post: {
      knowledge_id: string;
      query?: string;
      score_threshold: number;
      top_k: number;
    };
    Body_summary_doc_ids_to_vector_store_knowledge_base_kb_summary_api_summary_doc_ids_to_vector_store_post: {
      doc_ids?: unknown[];
      embed_model?: string;
      file_description?: string;
      knowledge_base_name: string;
      max_tokens?: number | null;
      model_name?: string;
      temperature?: number;
      vs_type?: string;
    };
    Body_summary_file_to_vector_store_knowledge_base_kb_summary_api_summary_file_to_vector_store_post: {
      allow_empty_kb?: boolean;
      embed_model?: string;
      file_description?: string;
      file_name: string;
      knowledge_base_name: string;
      max_tokens?: number | null;
      model_name?: string;
      temperature?: number;
      vs_type?: string;
    };
    Body_update_docs_knowledge_base_update_docs_post: {
      chunk_overlap?: number;
      chunk_size?: number;
      docs?: string;
      file_names: string[];
      knowledge_base_name: string;
      not_refresh_vs_cache?: boolean;
      override_custom_docs?: boolean;
      zh_title_enhance?: boolean;
    };
    Body_update_info_knowledge_base_update_info_post: {
      kb_info: string;
      knowledge_base_name: string;
    };
    Body_upload_attachment_supervision_projects__project_ref__attachments_post: {
      file: string;
      kind?: string;
      progress_ref?: string;
      submission_ref?: string;
      task_ref?: string;
    };
    Body_upload_docs_knowledge_base_upload_docs_post: {
      chunk_overlap?: number;
      chunk_size?: number;
      docs?: string;
      files: string[];
      knowledge_base_name: string;
      not_refresh_vs_cache?: boolean;
      override?: boolean;
      to_vector_store?: boolean;
      zh_title_enhance?: boolean;
    };
    Body_upload_temp_docs_knowledge_base_upload_temp_docs_post: {
      chunk_overlap?: number;
      chunk_size?: number;
      files: string[];
      prev_id?: string;
      zh_title_enhance?: boolean;
    };
    BriefBody: {
      content?: components["schemas"]["BriefContentIn"];
      conversation_id?: string;
      entry_type?: string;
      kind?: string;
      message_ids?: string[];
      title?: string;
    };
    BriefChangeOut: {
      added: string[] | null;
      field: string;
      from: string | string[];
      label: string;
      removed: string[] | null;
      to: string | string[];
    };
    BriefComparisonOut: {
      brief_id: string;
      changes: components["schemas"]["BriefChangeOut"][];
      code: string;
      from_confirmed_at: string | null;
      from_status: "draft" | "confirmed" | "rejected" | "superseded";
      from_version: number;
      to_confirmed_at: string | null;
      to_status: "draft" | "confirmed" | "rejected" | "superseded";
      to_version: number;
    };
    BriefContentIn: {
      background?: string | string[];
      constraints?: string | string[];
      expected_outcomes?: string | string[];
      hypotheses?: string | string[];
      keywords?: string[];
      methods?: string | string[];
      metrics?: string | string[];
      objectives?: string | string[];
      prior_attempts?: string | string[];
      question?: string | string[];
      scope?: string | string[];
      sub_questions?: string[];
      subjects?: string | string[];
      to_verify?: string | string[];
    };
    BriefOut: {
      background: string;
      brief_id: string;
      can_edit: boolean;
      code: string;
      confirm_note: string;
      confirmed_at: string | null;
      confirmed_by: string;
      constraints: string;
      create_time: string | null;
      current_version: number;
      display_code: string;
      entry_type: string;
      expected_outcomes: string;
      hypotheses: string;
      is_owner: boolean;
      keywords: string[];
      kind: string;
      methods: string;
      metrics: string;
      objectives: string;
      owner_role: string;
      owner_user_id: string;
      prior_attempts: string;
      project_id: string;
      question: string;
      scope: string;
      shared_version: number | null;
      shown_version: number;
      source_conversation_id: string | null;
      source_message_ids: string[];
      status: string;
      sub_questions: string[];
      subjects: string;
      title: string;
      to_verify: string;
      update_time: string | null;
      version_status: string;
    };
    BriefPatchBody: {
      content?: components["schemas"]["BriefContentIn"] | null;
      entry_type?: string | null;
      title?: string | null;
    };
    BriefVersionOut: {
      background: string;
      confirm_note: string;
      confirmed_at: string | null;
      confirmed_by: string;
      constraints: string;
      create_time: string | null;
      edited_fields: string[];
      entry_type: string;
      expected_outcomes: string;
      hypotheses: string;
      keywords: string[];
      methods: string;
      metrics: string;
      objectives: string;
      prior_attempts: string;
      question: string;
      scope: string;
      source_message_ids: string[];
      status: "draft" | "confirmed" | "rejected" | "superseded";
      sub_questions: string[];
      subjects: string;
      superseded_at: string | null;
      to_verify: string;
      version: number;
    };
    CancelBody: {
      reason?: string;
    };
    ChangePasswordRequest: {
      old_password: string;
      password: string;
    };
    ChatCompletionAllowedToolChoiceParam: {
      allowed_tools: components["schemas"]["ChatCompletionAllowedToolsParam"];
      type: "allowed_tools";
    };
    ChatCompletionAllowedToolsParam: {
      mode: "auto" | "required";
      tools: {
        [key: string]: unknown;
      }[];
    };
    ChatCompletionFunctionCallOptionParam: {
      name: string;
    };
    ChatCompletionFunctionToolParam: {
      function: components["schemas"]["FunctionDefinition"];
      type: "function";
    };
    ChatCompletionNamedToolChoiceCustomParam: {
      custom: components["schemas"]["Custom"];
      type: "custom";
    };
    ChatCompletionNamedToolChoiceParam: {
      function: components["schemas"]["openai__types__chat__chat_completion_named_tool_choice_param__Function"];
      type: "function";
    };
    CitationBody: {
      bound_object_code?: string;
      bound_object_type?: string;
      note?: string;
      purpose: string;
      source_kind: string;
      source_ref: string;
    };
    CitationOut: {
      bound_object_code: string;
      bound_object_type: string;
      citation_id: string;
      code: string;
      content_hash: string;
      create_time: string | null;
      granted_at: string | null;
      granted_by: string;
      granted_role: string;
      note: string;
      project_id: string;
      purpose: string;
      revoke_reason: string;
      revoked_at: string | null;
      revoked_by: string;
      snapshot: {
        [key: string]: unknown;
      };
      source_code: string;
      source_kind: string;
      source_owner_id: string;
      source_ref: string;
      source_version: string;
    };
    CompareBody: {
      result_refs?: string[];
    };
    ComparisonGenerateRequest: {
      detect_conflicts?: boolean;
      dimension_ids?: string[] | null;
      model?: string | null;
      run_id?: string | null;
    };
    CompletenessCheckOut: {
      key: "self_assessment" | "required_materials" | "code_material" | "runtime_material" | "unfinished_items";
      message: string;
      passed: boolean;
    };
    ConclusionBody: {
      confirm?: boolean;
      evidence_card_codes?: string[];
      research_gaps?: string[];
      statement: string;
      student_judgements?: {
        [key: string]: unknown;
      }[];
      supported_facts?: {
        [key: string]: unknown;
      }[];
      task_ref?: string;
      unverified_hypotheses?: {
        [key: string]: unknown;
      }[];
    };
    ConclusionOut: {
      code: string;
      conclusion_id: string;
      confirm_note: string;
      confirmed_at: string | null;
      confirmed_by: string;
      create_time: string | null;
      created_by: string;
      created_role: string;
      current_included_count: number;
      evidence_cards: {
        [key: string]: unknown;
      }[];
      literature_changed: boolean;
      literature_count: number;
      literature_snapshot: {
        [key: string]: unknown;
      }[];
      literature_snapshot_hash: string;
      mentor_at: string | null;
      mentor_by: string;
      mentor_note: string;
      mentor_role: string;
      mentor_state: string;
      project_id: string;
      research_gaps: string[];
      search_task_code: string;
      search_task_id: string | null;
      statement: string;
      status: string;
      student_judgements: {
        [key: string]: unknown;
      }[];
      superseded_at: string | null;
      superseded_by_code: string;
      superseded_by_version: number;
      supported_facts: {
        [key: string]: unknown;
      }[];
      unverified_hypotheses: {
        [key: string]: unknown;
      }[];
      update_time: string | null;
      version: number;
    };
    ConfirmBody: {
      note?: string;
    };
    ConfirmOut: {
      draft: components["schemas"]["DraftOut"];
      progress: components["schemas"]["chatchat__server__api_server__supervision_latex_routes__ProgressOut"];
    };
    ConflictResolve: {
      resolution: string;
    };
    ContextEdit: {
      completed_items?: string[] | null;
      confirmed?: string[] | null;
      constraints?: string[] | null;
      goal?: string | null;
      open_questions?: string[] | null;
      summary?: string | null;
      topic?: string | null;
    };
    ContextOut: {
      completed_items: string[];
      confirmed: string[];
      confirmed_saved: boolean[];
      constraints: string[];
      conversation_id: string;
      goal: string;
      needs_rebuild: boolean;
      open_questions: string[];
      project_id: string | null;
      recent_boundary_message_id: string | null;
      recent_window_size: number;
      recent_window_truncated: boolean;
      summarized_count: number;
      summarized_message_id: string | null;
      summary: string;
      summary_error: string | null;
      summary_status: string;
      summary_type: string;
      summary_updated_at: string | null;
      temporary: boolean;
      topic: string;
      update_time: string | null;
      version: number;
    };
    ConversationCreate: {
      chat_type?: string;
      name?: string;
      project_id?: string | null;
      temporary?: boolean;
    };
    ConversationDeletedOut: {
      conversation_id: string;
      deleted: boolean;
    };
    ConversationMessagesOut: {
      conversation_id: string;
      messages: components["schemas"]["MessageOut"][];
    };
    ConversationOut: {
      chat_type: string | null;
      create_time: string | null;
      id: string;
      name: string | null;
      project_id: string | null;
      temporary: boolean;
      update_time: string | null;
    };
    ConversationProject: {
      project_id?: string | null;
    };
    ConversationRename: {
      name?: string;
    };
    CreateProjectBody: {
      description?: string;
      name: string;
    };
    CreateUserRequest: {
      display_name: string;
      password: string;
      role?: string;
      username: string;
    };
    Custom: {
      name: string;
    };
    DecisionBody: {
      decision: string;
      reason: string;
    };
    DeleteProjectBody: {
      reason?: string;
    };
    DialogueBody: {
      chat_type?: string;
      title: string;
    };
    DialogueMessageBody: {
      text: string;
    };
    DialogueMessageOut: {
      create_time: string | null;
      message_id: string;
      metadata: {
        [key: string]: unknown;
      };
      query: string | null;
      response: string | null;
    };
    DialogueMessagesOut: {
      code: string;
      conversation_id: string;
      messages: components["schemas"]["DialogueMessageOut"][];
    };
    DialogueOut: {
      chat_type: string | null;
      code: string;
      conversation_id: string;
      create_time: string | null;
      message_count: number;
      name: string | null;
      supervision_project_id: string | null;
      update_time: string | null;
    };
    DimensionItem: {
      id: string;
      label?: string;
    };
    DimensionsUpdate: {
      dimensions?: components["schemas"]["DimensionItem"][];
    };
    DraftBody: {
      attachment_codes?: string[];
      core_attachment_codes?: string[];
      experiment_notes?: string;
      force?: boolean;
      git_branch?: string;
      git_commit?: string;
      git_repo?: string;
      known_issues?: string;
      self_assessment?: {
        [key: string]: unknown;
      }[];
      summary?: string;
      unfinished_items?: string;
    };
    DraftConfirmBody: {
      done?: string;
      materials?: string;
      milestone_ref?: string;
      next_steps?: string;
      problems?: string;
      results?: string;
      task_ref?: string;
    };
    DraftDiscardBody: {
      reason?: string;
    };
    DraftOut: {
      attachment_code: string;
      attachment_sha256: string;
      attachment_version: number;
      base_attachment_code: string;
      base_attachment_sha256: string;
      base_attachment_version: number;
      base_main_file: string;
      base_snapshot_code: string;
      baseline_only: boolean;
      caveat: string;
      checker_version: string;
      code: string;
      confirmed_at: string | null;
      confirmed_by: string;
      confirmed_progress_code: string;
      confirmed_role: string;
      counts: {
        [key: string]: unknown;
      };
      create_time: string | null;
      created_by: string;
      created_role: string;
      discard_reason: string;
      discarded_at: string | null;
      discarded_by: string;
      discarded_role: string;
      draft_id: string;
      draft_version: string;
      dropped_items: number;
      item_count: number;
      items: {
        [key: string]: unknown;
      }[];
      library_draft_id: string;
      main_file: string;
      model_name: string;
      model_summary: string;
      model_summary_note: string;
      model_summary_status: string;
      parser_version: string;
      project_id: string;
      published: boolean;
      requires_student_confirmation: boolean;
      snapshot_code: string;
      status: string;
      summary: string;
      truncated: boolean;
      update_time: string | null;
      word_count_delta: number;
      word_count_new: number;
      word_count_old: number;
      zip_safety_version: string;
    };
    EvidenceCardBody: {
      brief_field?: string;
      evidence_type?: string;
      extraction_id?: string;
      level?: string;
      locator?: {
        [key: string]: unknown;
      };
      quote: string;
      result_ref: string;
      sub_question?: string;
      summary?: string;
      support_relation?: string;
    };
    EvidenceCardOut: {
      brief_field: string;
      card_id: string;
      code: string;
      create_time: string | null;
      created_by: string;
      created_role: string;
      evidence_type: string;
      extraction_id: string | null;
      extraction_version: number;
      level: string;
      locator: {
        [key: string]: unknown;
      };
      project_id: string;
      quote: string;
      quote_verified: boolean;
      result_code: string;
      result_id: string;
      sub_question: string;
      summary: string;
      support_relation: string;
      updated_at: string | null;
    };
    EvidenceExtractRequest: {
      model?: string | null;
      question?: string | null;
      run_id?: string | null;
      sub_question: string;
      sub_question_id: string;
    };
    EvidenceReview: {
      note?: string;
      quote?: string | null;
      review_status: string;
      summary?: string | null;
    };
    ExtractBody: {
      brief_ref?: string;
      conversation_id?: string;
      message_ids?: string[];
      title?: string;
    };
    ExtractionOut: {
      algorithm: string;
      create_time: string | null;
      created_by: string;
      created_role: string;
      differences: {
        [key: string]: unknown;
      };
      evidence: {
        [key: string]: unknown;
      }[];
      experiments: string;
      extraction_id: string;
      level: string;
      limitations: string;
      method: string;
      model_name: string;
      project_id: string;
      prompt_version: string;
      research_problem: string;
      result_code: string;
      result_id: string;
      results_metrics: string;
      subtask_code: string;
      subtask_id: string;
      summary: string;
      superseded_at: string | null;
      version: number;
    };
    FindingOut: {
      ac_code: string;
      ac_statement: string;
      ac_version: number;
      batch_index: number;
      check_type: string;
      downgrade_reason: string;
      evidence_depth: string;
      evidence_refs: {
        [key: string]: unknown;
      }[];
      finding_id: string;
      injection_note: string;
      injection_suspected: boolean;
      mentor_focus: string;
      missing_materials: string[];
      missing_parts: string;
      reason: string;
      required: boolean;
      risks: {
        [key: string]: unknown;
      }[];
      run_code: string;
      run_id: string;
      runtime_claim_flag: boolean;
      satisfied_parts: string;
      support_relation: string;
      validated: boolean;
      verdict: string;
    };
    FullTextRequest: {
      kb_names?: string[] | null;
      run_id?: string | null;
    };
    FunctionDefinition: {
      description?: string;
      name: string;
      parameters?: {
        [key: string]: unknown;
      };
      strict?: boolean | null;
    };
    HTTPValidationError: {
      detail?: components["schemas"]["ValidationError"][];
    };
    History: {
      content: string;
      role: string;
    };
    IgnoreRequest: {
      memory_id: string;
    };
    IntentApplyRequest: {
      category?: string | null;
      content: string;
      conversation_id?: string | null;
      kind: string;
      scope: string;
      target_ids?: string[] | null;
    };
    IntentDetectRequest: {
      conversation_id?: string | null;
      text: string;
    };
    JSONSchema: {
      description?: string;
      name: string;
      schema?: {
        [key: string]: unknown;
      };
      strict?: boolean | null;
    };
    KBDefaultsOut: {
      chunk_overlap: number;
      chunk_size: number;
      default_knowledge_base: string;
      default_search_engine: string | null;
      default_vs_type: string;
      score_threshold: number;
      vector_search_top_k: number;
    };
    ListResponse: {
      code: number;
      data: unknown[];
      msg: string;
    };
    LoginRequest: {
      password: string;
      username: string;
    };
    LoginResponse: {
      expires_in: number;
      token: string;
      token_type: string;
      user: components["schemas"]["UserOut"];
    };
    MCPConnectionCreate: {
      args?: string[];
      config?: {
        [key: string]: unknown;
      };
      cwd?: string | null;
      description?: string | null;
      enabled?: boolean;
      env?: {
        [key: string]: string;
      };
      server_name: string;
      timeout?: number;
      transport?: string;
    };
    MCPConnectionListResponse: {
      connections: components["schemas"]["MCPConnectionResponse"][];
      total: number;
    };
    MCPConnectionResponse: {
      args: string[];
      config: {
        [key: string]: unknown;
      };
      create_time: string;
      cwd: string | null;
      description: string | null;
      enabled: boolean;
      env: {
        [key: string]: string;
      };
      id: string;
      server_name: string;
      timeout: number;
      transport: string;
      update_time: string | null;
    };
    MCPConnectionSearchRequest: {
      enabled?: boolean | null;
      keyword?: string | null;
      limit?: number;
      transport?: string | null;
    };
    MCPConnectionStatusResponse: {
      connection_id: string | null;
      message: string;
      success: boolean;
    };
    MCPConnectionUpdate: {
      args?: string[] | null;
      config?: {
        [key: string]: unknown;
      } | null;
      cwd?: string | null;
      description?: string | null;
      enabled?: boolean | null;
      env?: {
        [key: string]: string;
      } | null;
      server_name?: string | null;
      timeout?: number | null;
      transport?: string | null;
    };
    MCPProfileCreate: {
      env_vars?: {
        [key: string]: string;
      };
      timeout?: number;
      working_dir?: string;
    };
    MCPProfileResponse: {
      env_vars: {
        [key: string]: string;
      };
      timeout: number;
      update_time: string;
      working_dir: string;
    };
    MCPProfileStatusResponse: {
      message: string;
      success: boolean;
    };
    MarkReadOut: {
      updated: number;
    };
    MaterialIn: {
      key: string;
      label?: string;
      required?: boolean;
    };
    MaterialOut: {
      key: string;
      label: string;
      required: boolean;
    };
    MemoryCreate: {
      category?: string | null;
      confirmed?: boolean | null;
      content: string;
      expires_at?: string | null;
      importance?: number;
      kind?: string | null;
      project_id?: string | null;
      scope?: string | null;
      source?: string;
      type?: string;
    };
    MemoryOut: {
      category: string | null;
      confirmed: boolean;
      content: string;
      create_time: unknown | null;
      enabled: boolean;
      expires_at: unknown | null;
      id: string;
      importance: number;
      kind: string;
      memory_key: string;
      project_id: string | null;
      project_name: string | null;
      scope: string;
      source: string;
      source_conversation_id: string | null;
      source_label: string;
      source_message_id: string | null;
      status: string;
      superseded_by: string | null;
      supersedes: string | null;
      type: string;
      update_time: unknown | null;
    };
    MemoryUpdate: {
      category?: string | null;
      confirmed?: boolean | null;
      content?: string | null;
      enabled?: boolean | null;
      expires_at?: string | null;
      importance?: number | null;
      kind?: string | null;
      project_id?: string | null;
      scope?: string | null;
      source?: string | null;
      type?: string | null;
    };
    MentorDiffOut: {
      ac_code: string;
      ai_relation: string;
      consistent: boolean;
      mentor_conclusion: string;
      suggested_conclusion: string;
      verdict: string;
    };
    MentorReviewBody: {
      note?: string;
      state: string;
    };
    MentorRunBody: {
      core_attachment_codes?: string[];
    };
    MessageOut: {
      chat_type: string | null;
      create_time: string | null;
      feedback_reason: string | null;
      feedback_score: number | null;
      id: string;
      metadata: {
        [key: string]: unknown;
      };
      query: string | null;
      response: string | null;
    };
    MilestoneBody: {
      description?: string;
      owner_user_id?: string;
      planned_end?: string | null;
      planned_start?: string | null;
      success_criteria?: string;
      title: string;
    };
    MilestoneConfirmBody: {
      confirmation: string;
      note?: string;
    };
    MilestoneOut: {
      code: string;
      create_time: string | null;
      description: string;
      mentor_confirmation: "" | "confirmed" | "partially_confirmed" | "not_confirmed";
      mentor_confirmation_note: string;
      mentor_confirmed_at: string | null;
      milestone_id: string;
      owner_user_id: string;
      planned_end: string | null;
      planned_start: string | null;
      status: "planning" | "active" | "completed" | "cancelled";
      success_criteria: string;
      title: string;
    };
    MilestonePatchBody: {
      description?: string | null;
      owner_user_id?: string | null;
      planned_end?: string | null;
      planned_start?: string | null;
      status?: string | null;
      success_criteria?: string | null;
      title?: string | null;
    };
    NotificationOut: {
      body: string;
      created_at: string | null;
      event_code: string;
      event_type: string;
      level: string;
      notification_id: string;
      object_code: string;
      object_type: string;
      project_id: string;
      read: boolean;
      read_at: string | null;
      title: string;
    };
    OpenAIAudioSpeechInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      input: string;
      model: string;
      response_format?: "mp3" | "opus" | "aac" | "flac" | "pcm" | "wav" | null;
      speed?: number | null;
      timeout?: number | null;
      user?: string | null;
      voice: string;
      [key: string]: unknown;
    };
    OpenAIAudioTranscriptionsInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      file: string | string;
      language?: string | null;
      model: string;
      prompt?: string | null;
      response_format?: string | null;
      temperature?: number;
      timeout?: number | null;
      timestamp_granularities?: Array<"word" | "segment"> | null;
      user?: string | null;
      [key: string]: unknown;
    };
    OpenAIAudioTranslationsInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      file: string | string;
      model: string;
      prompt?: string | null;
      response_format?: string | null;
      temperature?: number;
      timeout?: number | null;
      user?: string | null;
      [key: string]: unknown;
    };
    OpenAIChatInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      frequency_penalty?: number | null;
      function_call?: "none" | "auto" | components["schemas"]["ChatCompletionFunctionCallOptionParam"] | null;
      functions?: components["schemas"]["openai__types__chat__completion_create_params__Function"][];
      logit_bias?: {
        [key: string]: number;
      } | null;
      logprobs?: boolean | null;
      max_tokens?: number | null;
      messages: {
        [key: string]: unknown;
      }[];
      model?: string;
      n?: number | null;
      presence_penalty?: number | null;
      response_format?: components["schemas"]["ResponseFormatText"] | components["schemas"]["ResponseFormatJSONSchema"] | components["schemas"]["ResponseFormatJSONObject"];
      seed?: number | null;
      stop?: string | string[] | null;
      stream?: boolean | null;
      temperature?: number | null;
      timeout?: number | null;
      tool_choice?: "none" | "auto" | "required" | components["schemas"]["ChatCompletionAllowedToolChoiceParam"] | components["schemas"]["ChatCompletionNamedToolChoiceParam"] | components["schemas"]["ChatCompletionNamedToolChoiceCustomParam"] | string | null;
      tools?: Array<components["schemas"]["ChatCompletionFunctionToolParam"] | string>;
      top_logprobs?: number | null;
      top_p?: number | null;
      user?: string | null;
      [key: string]: unknown;
    };
    OpenAIEmbeddingsInput: {
      dimensions?: number | null;
      encoding_format?: "float" | "base64" | null;
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      input: string | string[];
      model: string;
      timeout?: number | null;
      user?: string | null;
      [key: string]: unknown;
    };
    OpenAIImageEditsInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      image: string | string;
      mask: string | string;
      model: string;
      n?: number;
      prompt: string;
      response_format?: "url" | "b64_json" | null;
      size?: "256x256" | "512x512" | "1024x1024" | "1792x1024" | "1024x1792" | null;
      timeout?: number | null;
      user?: string | null;
      [key: string]: unknown;
    };
    OpenAIImageGenerationsInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      model: string;
      n?: number;
      prompt: string;
      quality?: "standard" | "hd";
      response_format?: "url" | "b64_json" | null;
      size?: "256x256" | "512x512" | "1024x1024" | "1792x1024" | "1024x1792" | null;
      style?: "vivid" | "natural" | null;
      timeout?: number | null;
      user?: string | null;
      [key: string]: unknown;
    };
    OpenAIImageVariationsInput: {
      extra_body?: {
        [key: string]: unknown;
      } | null;
      extra_headers?: {
        [key: string]: unknown;
      } | null;
      extra_query?: {
        [key: string]: unknown;
      } | null;
      image: string | string;
      model: string;
      n?: number;
      response_format?: "url" | "b64_json" | null;
      size?: "256x256" | "512x512" | "1024x1024" | "1792x1024" | "1024x1792" | null;
      timeout?: number | null;
      user?: string | null;
      [key: string]: unknown;
    };
    PlanUpdate: {
      plan: {
        [key: string]: unknown;
      };
    };
    PlatformModelsOut: {
      embed_models: string[];
      image2text_models: string[];
      llm_models: string[];
      name: string;
    };
    PreferencesOut: {
      enabled: boolean;
      max_count: number;
      preferences: components["schemas"]["MemoryOut"][];
    };
    ProgressBody: {
      done?: string;
      links?: {
        [key: string]: unknown;
      }[];
      materials?: string;
      milestone_ref?: string;
      next_steps?: string;
      problems?: string;
      results?: string;
      task_ref?: string;
    };
    ProgressDraftBody: {
      attachment_ref: string;
      base_attachment_ref?: string;
      base_main_file?: string;
      main_file?: string;
    };
    ProgressNoteOut: {
      author_id: string;
      author_role: string;
      create_time: string | null;
      note: string;
      note_id: string;
    };
    ProjectContextOut: {
      context: string;
      has_project_context: boolean;
      project_id: string;
    };
    ProjectCreate: {
      name: string;
      research_question: string;
      scope?: string;
    };
    ProjectDetailOut: {
      archive_reject_reason: string;
      archive_request_reason: string;
      archive_requested_at: string | null;
      archived_at: string | null;
      code: string;
      create_time: string | null;
      description: string;
      members: components["schemas"]["ProjectMemberOut"][];
      my_role: string | null;
      name: string;
      project_id: string;
      status: string;
      update_time: string | null;
    };
    ProjectMemberOut: {
      joined_at: string | null;
      removed_at: string | null;
      role: "student" | "mentor";
      status: "active" | "removed";
      user_id: string;
    };
    ProjectOut: {
      archive_reject_reason: string;
      archive_request_reason: string;
      archive_requested_at: string | null;
      archived_at: string | null;
      code: string;
      create_time: string | null;
      description: string;
      my_role: string | null;
      name: string;
      project_id: string;
      status: string;
      update_time: string | null;
    };
    ProjectUpdate: {
      name?: string | null;
      research_question?: string | null;
      scope?: string | null;
      status?: string | null;
    };
    PromoteRequest: {
      content: string;
      conversation_id?: string | null;
      expires_at?: string | null;
      kind?: string;
      project_id?: string | null;
      scope?: string;
    };
    ReadNotificationsBody: {
      mark_all?: boolean;
      notification_id?: string;
    };
    ReasonBody: {
      reason: string;
    };
    RecallPreviewOut: {
      conversation_id: string | null;
      max_count: number;
      memories: {
        [key: string]: unknown;
      }[];
      project_id: string | null;
      switches: {
        [key: string]: unknown;
      };
    };
    RecallSwitchesUpdate: {
      decisions?: boolean | null;
      enabled?: boolean | null;
      general?: boolean | null;
      preferences?: boolean | null;
      project?: boolean | null;
    };
    ReportEdit: {
      content: string;
      title?: string | null;
    };
    ReportGenerateRequest: {
      model?: string | null;
      run_id?: string | null;
    };
    ResetPasswordRequest: {
      password: string;
    };
    ResponseFormatJSONObject: {
      type: "json_object";
    };
    ResponseFormatJSONSchema: {
      json_schema: components["schemas"]["JSONSchema"];
      type: "json_schema";
    };
    ResponseFormatText: {
      type: "text";
    };
    ResultOut: {
      abstract: string;
      arxiv_id: string | null;
      authors: string;
      code: string;
      create_time: string | null;
      decided_at: string | null;
      decided_by: string;
      decided_role: string;
      decision: string;
      decision_history: {
        [key: string]: unknown;
      }[];
      decision_reason: string;
      dedup_key: string;
      doi: string | null;
      evidence_cards: components["schemas"]["EvidenceCardOut"][];
      extract_state: string;
      first_seen_at: string | null;
      fulltext_status: string;
      latest_extraction_version: number;
      project_id: string;
      recommended_action: string;
      relevance_level: string;
      relevance_matched: {
        [key: string]: unknown;
      }[];
      relevance_missing: {
        [key: string]: unknown;
      }[];
      relevance_reason: string;
      relevance_rules_version: string;
      relevance_score: number | null;
      relevance_score_source: string;
      result_id: string;
      search_task_id: string;
      semantic_analysis: {
        [key: string]: unknown;
      };
      source: string;
      sources: {
        [key: string]: unknown;
      }[];
      subtask_id: string;
      title: string;
      title_key: string | null;
      update_time: string | null;
      url: string | null;
      year: number | null;
    };
    ReviewAcOut: {
      ac_code: string;
      ai_relation: string;
      conclusion: "met" | "partially_met" | "not_met" | "more_evidence_required" | "not_applicable";
      reason: string;
    };
    ReviewDraftBody: {
      conclusions?: components["schemas"]["AcConclusionIn"][];
      feedback?: string;
      must_fix?: string[] | null;
      next_steps?: string;
      suggestions?: string[] | null;
    };
    ReviewOut: {
      code: string;
      conclusions: components["schemas"]["ReviewAcOut"][];
      decision_reason: string;
      feedback: string;
      first_touched_at: string | null;
      must_fix: string[];
      next_steps: string;
      notes: components["schemas"]["ProgressNoteOut"][];
      overall_decision: "" | "approved" | "revision_required" | "rejected" | "paused";
      published_at: string | null;
      review_id: string;
      reviewer_id: string;
      status: "not_started" | "drafting" | "published";
      submission_id: string;
      suggestions: string[];
      task_id: string;
    };
    ReviewPublishBody: {
      conclusions?: components["schemas"]["AcConclusionIn"][];
      decision: string;
      feedback?: string;
      must_fix?: string[] | null;
      next_steps?: string;
      reason?: string;
      suggestions?: string[] | null;
    };
    RunOut: {
      ac_version: number;
      attach_allowed: boolean;
      attached_to_submission: boolean;
      budget_config_version: string;
      call_plan: {
        [key: string]: unknown;
      }[];
      cancel_reason: string;
      cancelled_at: string | null;
      cancelled_by: string;
      check_snapshot: {
        [key: string]: unknown;
      };
      code: string;
      completed_batches: number;
      create_time: string | null;
      display_code: string;
      draft_fingerprint: string;
      failure_reason: string;
      finding_counts: {
        [key: string]: unknown;
      };
      finished_at: string | null;
      input_budget: {
        [key: string]: unknown;
      };
      material_manifest: {
        [key: string]: unknown;
      }[];
      model_available: boolean;
      model_calls: {
        [key: string]: unknown;
      }[];
      model_name: string;
      parser_version: string;
      partial_results: boolean;
      partially_read_materials: {
        [key: string]: unknown;
      }[];
      project_id: string;
      queue_status: {
        [key: string]: unknown;
      };
      queued_at: string | null;
      revision: number;
      rules_version: string;
      run_id: string;
      security_flags: string[];
      stale: boolean;
      stale_label: string;
      stale_reasons: string[];
      started_at: string | null;
      status: string;
      submission_code: string;
      submission_display: string;
      submission_id: string | null;
      submission_version: number;
      summary_note: string;
      summary_state: string;
      task_code: string;
      task_id: string;
      total_batches: number;
      trigger_source: string;
      triggered_by: string;
      triggered_role: string;
      unchecked_acs: string[];
      unread_materials: {
        [key: string]: unknown;
      }[];
      visible_to_mentor: boolean;
      whitelist_version: string;
    };
    ScreeningUpdate: {
      reason?: string;
      screening_status: string;
    };
    SearchBody: {
      keyword_batches?: {
        [key: string]: unknown;
      }[] | null;
      sources?: string[] | null;
    };
    SelfAssessmentIn: {
      ac_code: string;
      evidence?: string;
      note?: string;
      self_assessment: string;
    };
    SelfAssessmentOut: {
      ac_code: string;
      evidence: string;
      note: string;
      self_assessment: "done" | "partially_done" | "not_done" | "not_applicable";
    };
    ServerModelsOut: {
      default_embedding: string;
      default_llm: string;
      embedding_models: string[];
      history_len: number;
      image2text_models: string[];
      kb_defaults: components["schemas"]["KBDefaultsOut"];
      llm_model_config: {
        [key: string]: {
          [key: string]: unknown;
        };
      };
      llm_models: string[];
      max_tokens: number | null;
      platforms: components["schemas"]["PlatformModelsOut"][];
      search_engines: string[];
      support_agent_models: string[];
      temperature: number;
      vs_types: string[];
    };
    SetStatusRequest: {
      status: string;
    };
    SettingsBody: {
      enabled?: boolean | null;
      mode?: string;
      task_ref?: string;
    };
    SettingsOut: {
      auto_review_enabled: boolean;
      default_is_off: boolean;
      effective_mode: string;
      priority_note: string;
      project_id: string;
      project_mode: string;
      task_id: string;
      task_mode: string;
    };
    SnapshotOut: {
      attachment_code: string;
      attachment_name: string;
      attachment_sha256: string;
      attachment_version: number;
      available_files: string[];
      candidates: {
        [key: string]: unknown;
      }[];
      check: {
        [key: string]: unknown;
      } | null;
      checker_version: string;
      code: string;
      compiled: boolean;
      counts: {
        [key: string]: unknown;
      };
      create_time: string | null;
      created_by: string;
      created_role: string;
      dangerous_findings: {
        [key: string]: unknown;
      }[];
      document: {
        [key: string]: unknown;
      } | null;
      document_present: boolean;
      extracted_files: {
        [key: string]: unknown;
      }[];
      file_count: number;
      finding_counts: {
        [key: string]: unknown;
      };
      findings: {
        [key: string]: unknown;
      }[];
      limits: {
        [key: string]: unknown;
      };
      main: {
        [key: string]: unknown;
      };
      main_file: string;
      main_path: string;
      main_status: string;
      not_read: {
        [key: string]: unknown;
      }[];
      outline: string[];
      parse_budget: {
        [key: string]: unknown;
      };
      parser_version: string;
      partially_read: {
        [key: string]: unknown;
      }[];
      project_id: string;
      snapshot_id: string;
      total_bytes: number;
      unreferenced_tex: string[];
      word_count: number;
      zip_safety_version: string;
    };
    StalenessOut: {
      attach_allowed: boolean;
      based_on: string;
      label: string;
      reasons: string[];
      rerun_allowed: boolean;
      run_code: string;
      snapshot_fingerprint: string;
      stale: boolean;
    };
    SubmissionBody: {
      attachment_codes?: string[];
      experiment_notes?: string;
      git_branch?: string;
      git_commit?: string;
      git_repo?: string;
      known_issues?: string;
      self_assessment?: components["schemas"]["SelfAssessmentIn"][];
      summary?: string;
      unfinished_items?: string;
    };
    SubmissionMaterialOut: {
      attachment_code: string;
      kind: string;
      name: string;
      sha256: string;
      size: number;
      version: number;
    };
    SubmissionOut: {
      ac_snapshot: components["schemas"]["AcOut"][];
      author_id: string;
      code: string;
      completeness_check: components["schemas"]["CompletenessCheckOut"][];
      create_time: string | null;
      display_code: string;
      experiment_notes: string;
      git_branch: string;
      git_commit: string;
      git_repo: string;
      known_issues: string;
      materials: components["schemas"]["SubmissionMaterialOut"][];
      self_assessment: components["schemas"]["SelfAssessmentOut"][];
      status: "submitted" | "withdrawn" | "superseded";
      submission_id: string;
      summary: string;
      task_id: string;
      unfinished_items: string;
      version: number;
      withdraw_reason: string;
      withdrawn_at: string | null;
    };
    SubtaskOut: {
      attempt: number;
      cancel_reason: string;
      cancelled_at: string | null;
      cancelled_by: string;
      code: string;
      consumer: string;
      create_time: string | null;
      created_by: string;
      created_role: string;
      duplicate_total: number;
      error_code: string;
      error_message: string;
      failure_history: {
        [key: string]: unknown;
      }[];
      finished_at: string | null;
      kind: string;
      model_calls: {
        [key: string]: unknown;
      }[];
      model_name: string;
      new_total: number;
      payload: {
        [key: string]: unknown;
      };
      prompt_version: string;
      query: string;
      queued_at: string | null;
      result_total: number;
      source: string;
      started_at: string | null;
      status: string;
      subtask_id: string;
      update_time: string | null;
    };
    SuggestionOut: {
      ac_code: string;
      note: string;
      reason: string;
      suggested_conclusion: string;
      suggested_label: string;
      verdict: string;
    };
    SupervisionUserOut: {
      display_name: string;
      user_id: string;
      username: string;
    };
    UserOut: {
      create_time: string | null;
      display_name: string;
      id: string;
      must_change_password: boolean;
      role: string;
      status: string;
      update_time: string | null;
      username: string;
    };
    ValidationError: {
      loc: Array<string | number>;
      msg: string;
      type: string;
    };
    chatchat__server__api_server__supervision_latex_routes__ProgressOut: {
      author_id: string;
      author_role: string;
      code: string;
      create_time: string | null;
      done: string;
      links: {
        [key: string]: unknown;
      }[];
      materials: string;
      next_steps: string;
      problems: string;
      progress_id: string;
      project_id: string;
      results: string;
      task_id: string | null;
    };
    chatchat__server__api_server__supervision_literature_routes__NoteBody: {
      note?: string;
    };
    chatchat__server__api_server__supervision_literature_routes__TaskBody: {
      as_draft?: boolean;
      brief_ref: string;
      deep_extract_limit?: number | null;
      exclude_terms?: string[];
      goal: string;
      keywords?: string[];
      purpose?: string;
      require_fulltext?: boolean;
      result_limit?: number;
      sources?: string[];
      synonyms?: string[];
      year_from?: number | null;
      year_to?: number | null;
    };
    chatchat__server__api_server__supervision_literature_routes__TaskOut: {
      brief_code: string;
      brief_confirmed_at: string | null;
      brief_confirmed_by: string;
      brief_id: string;
      brief_kind: string;
      brief_latest_version: number | null;
      brief_shared_version: number | null;
      brief_snapshot: {
        [key: string]: unknown;
      };
      brief_superseded: boolean;
      brief_version: number;
      close_reason: string;
      closed_at: string | null;
      code: string;
      concluded_at: string | null;
      conclusion_total: number;
      create_time: string | null;
      created_by: string;
      created_role: string;
      deduped_total: number;
      deep_extract_limit: number;
      deep_extract_used: number;
      exclude_terms: string[];
      excluded_total: number;
      goal: string;
      hits_total: number;
      included_total: number;
      keywords: string[];
      plan_error: string;
      plan_status: string;
      project_id: string;
      purpose: string;
      read_later_total: number;
      recent_subtasks: components["schemas"]["SubtaskOut"][];
      require_fulltext: boolean;
      result_limit: number;
      result_total: number;
      search_plan: {
        [key: string]: unknown;
      };
      search_plan_confirmed_at: string | null;
      search_plan_confirmed_by: string;
      search_plan_generated_at: string | null;
      search_plan_model: string;
      search_plan_note: string;
      search_plan_prompt_version: string;
      searched_at: string | null;
      source_failures: {
        [key: string]: unknown;
      }[];
      sources: string[];
      status: string;
      stored_total: number;
      subtask_failed_total: number;
      subtask_total: number;
      synonyms: string[];
      task_id: string;
      undecided_total: number;
      update_time: string | null;
      year_from: number | null;
      year_to: number | null;
    };
    chatchat__server__api_server__supervision_literature_routes__TaskPatchBody: {
      deep_extract_limit?: number | null;
      exclude_terms?: string[] | null;
      goal?: string | null;
      keywords?: string[] | null;
      purpose?: string | null;
      require_fulltext?: boolean | null;
      result_limit?: number | null;
      sources?: string[] | null;
      synonyms?: string[] | null;
      year_from?: number | null;
      year_to?: number | null;
    };
    chatchat__server__api_server__supervision_work_routes__NoteBody: {
      kind?: string;
      note: string;
    };
    chatchat__server__api_server__supervision_work_routes__ProgressOut: {
      author_id: string;
      author_role: string;
      code: string;
      create_time: string | null;
      done: string;
      links: {
        [key: string]: unknown;
      }[];
      materials: string;
      milestone_id: string | null;
      next_steps: string;
      notes: components["schemas"]["ProgressNoteOut"][];
      problems: string;
      progress_id: string;
      results: string;
      task_id: string | null;
    };
    chatchat__server__api_server__supervision_work_routes__TaskBody: {
      acs?: components["schemas"]["AcIn"][];
      description?: string;
      due_at?: string | null;
      milestone_id?: string;
      priority?: string;
      publish?: boolean;
      purpose: string;
      required_materials?: components["schemas"]["MaterialIn"][];
      requires_code_review?: boolean;
      requires_runtime?: boolean;
      title: string;
    };
    chatchat__server__api_server__supervision_work_routes__TaskOut: {
      acs: components["schemas"]["AcOut"][];
      cancel_reason: string;
      code: string;
      create_time: string | null;
      description: string;
      due_at: string | null;
      label: string;
      milestone_id: string | null;
      priority: "low" | "normal" | "high";
      published_at: string | null;
      purpose: string;
      required_materials: components["schemas"]["MaterialOut"][];
      requires_code_review: boolean;
      requires_runtime: boolean;
      status: "draft" | "active" | "completed" | "cancelled" | "archived";
      task_id: string;
      title: string;
      version: number;
    };
    chatchat__server__api_server__supervision_work_routes__TaskPatchBody: {
      acs?: components["schemas"]["AcIn"][] | null;
      description?: string | null;
      due_at?: string | null;
      milestone_id?: string | null;
      priority?: string | null;
      purpose?: string | null;
      required_materials?: components["schemas"]["MaterialIn"][] | null;
      requires_code_review?: boolean | null;
      requires_runtime?: boolean | null;
      title?: string | null;
    };
    chatchat__server__types__server__response__base__BaseResponse: {
      code: number;
      data: unknown | null;
      msg: string;
    };
    chatchat__server__utils__BaseResponse: {
      code: number;
      data: unknown;
      msg: string;
    };
    openai__types__chat__chat_completion_named_tool_choice_param__Function: {
      name: string;
    };
    openai__types__chat__completion_create_params__Function: {
      description?: string;
      name: string;
      parameters?: {
        [key: string]: unknown;
      };
    };
  };
}

export type Schema<N extends keyof components["schemas"]> = components["schemas"][N];
