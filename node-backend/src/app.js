import express from "express";
import vulcanRoutes from "./modules/vulcan/vulcan.routes.js";
import librusRoutes from "./modules/librus/librus.routes.js";


const app = express();
app.use(express.json());


app.use("/api/vulcan", vulcanRoutes);
app.use("/api/librus", librusRoutes);


export default app;