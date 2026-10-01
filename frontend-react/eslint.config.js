// ESLint 只管 React Hooks 的两条规则（作者拍板 #26b）：rules-of-hooks 是错误（CI 挡住），exhaustive-deps 先作警告
// （打印出来，不挡 CI）。别的规则一条不开，所以也不要写点名别的规则的 eslint-disable 注释：点名没开的规则、
// 或者已经压不住任何报告的 eslint-disable 注释本身就报错（reportUnusedDisableDirectives），注释不会再悄悄过期。
// 运行：npm run lint（CI 前端任务里也跑这一步）。
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
];
