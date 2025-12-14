import { Router } from "express";
import * as controller from "./librus.controller.js";


const router = Router();


router.post("/login", controller.login);
router.get("/grades", controller.grades);


export default router;