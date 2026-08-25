/**
 * 选择控件。**界面上所有"选一个"都走这里**（S134）。
 *
 * 判据只有一条：**取值是一组里选一个，交互形态就只有一种**——一排按钮，
 * 选中的高亮。二值也不例外。
 *
 * 曾经二值用的是"单个按钮、点一下自己翻面"的 toggle，理由是"它是开关不是选择"。
 * **那个区分是假的**：开关就是在「是」和「否」之间选一个，凭什么它要有另一套
 * 交互。真实代价有两层——
 *
 * - **手上的**：同一个页面里，有的地方点一下换值、有的地方点哪个是哪个，
 *   用户得先分辨这是哪一种才敢点。
 * - **眼睛上的**：toggle 只显示**当前值**，看不见"另一个选项是什么"。
 *   `is_player` 那处因此还多出一层误导——单按钮的形状暗示"可以多标几个"，
 *   而它其实是一份设定里至多一个。
 */

/** 从一组取值里选一个。 */
export function Pick<T>({
  value,
  options,
  onChange,
  disabled,
}: {
  value: T;
  /** `[取值, 显示名]`。顺序就是显示顺序。 */
  options: [T, string][];
  onChange: (next: T) => void;
  disabled?: boolean;
}): JSX.Element {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map(([option, label]) => (
        <button
          key={String(option)}
          onClick={() => onChange(option)}
          disabled={disabled}
          className={`btn ${value === option ? "border-accent text-fg" : ""}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

/** 是 / 否。**`Pick<boolean>` 的特化，不是另一种控件**——开关也是选择。 */
export function YesNo({
  value,
  onChange,
  yes = "是",
  no = "否",
  disabled,
}: {
  value: boolean;
  onChange: (next: boolean) => void;
  yes?: string;
  no?: string;
  disabled?: boolean;
}): JSX.Element {
  return (
    <Pick<boolean>
      value={value}
      options={[
        [true, yes],
        [false, no],
      ]}
      onChange={onChange}
      disabled={disabled}
    />
  );
}
