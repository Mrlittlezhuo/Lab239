/**
 * 参考资料展示（钉住「出处一行、片段按需展开」这条要求，全部离线）。
 *
 * 现场：同一个 PDF 被切成多段，参考资料里出现两条同名 2504.19413.pdf，
 * 只有文件名时用户分不清各自命中了什么；服务端其实一直带了 content。
 * 用户要求：没点「展开」之前不要显示片段。
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ReferenceList } from "@/components/chat/ReferenceList";

const LONG = "记忆机制让系统能够跨会话保存并检索历史信息。".repeat(12);

describe("ReferenceList", () => {
  it("收起时只显示出处：文件、知识库、页码、相关度", () => {
    render(
      <ReferenceList
        references={[
          {
            index: 1,
            file_name: "2310.11511.pdf",
            knowledge_base: "论文库",
            page: 4,
            score: 0.62,
            content: "记忆机制的定义：跨会话保存与检索历史信息的能力。",
          },
        ]}
      />,
    );

    expect(screen.getByText(/2310\.11511\.pdf/)).toBeInTheDocument();
    expect(
      screen.getByText(/知识库 论文库 · 第 4 页 · 相关度 0.620/),
    ).toBeInTheDocument();
    // 未展开不渲染片段
    expect(
      screen.queryByText("记忆机制的定义：跨会话保存与检索历史信息的能力。"),
    ).not.toBeInTheDocument();
  });

  it("点展开才出现片段，再点收起又消失", () => {
    render(<ReferenceList references={[{ index: 1, content: LONG }]} />);

    expect(screen.queryByText(LONG)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /展开/ }));
    expect(screen.getByText(LONG)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /收起/ }));
    expect(screen.queryByText(LONG)).not.toBeInTheDocument();
  });

  it("没有片段的引用展开后给出说明，不留空白", () => {
    render(
      <ReferenceList
        references={[{ index: 1, file_name: "search-result", url: "/x" }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /展开/ }));

    expect(screen.getByText(/该引用没有返回片段内容/)).toBeInTheDocument();
  });

  it("同名文件的不同片段各自独立展开", () => {
    render(
      <ReferenceList
        references={[
          { index: 2, file_name: "2504.19413.pdf", content: "第二段命中内容" },
          { index: 3, file_name: "2504.19413.pdf", content: "第三段命中内容" },
        ]}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: /展开/ })[0]);

    expect(screen.getByText("第二段命中内容")).toBeInTheDocument();
    expect(screen.queryByText("第三段命中内容")).not.toBeInTheDocument();
  });
});
