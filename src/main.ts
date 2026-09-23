// BatanaTool 前端入口：页面装配与路由在 ui/ 下，此处只做挂载。
import { mountApp } from "./ui/app";
import "./styles/tokens.css";
import "./styles/app.css";

mountApp(document.getElementById("app")!);
