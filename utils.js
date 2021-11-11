import nodemailer from "nodemailer";
import fs from "fs";
import fsp from "fs/promises";
import git from "isomorphic-git";
import http from "isomorphic-git/http/node";
import path from "path";

let lastEmailSent = 0;

export const sendEmail = async function (text) {
  if (new Date().getTime() - lastEmailSent < 30 * 60 * 1000) {
    throw new Error("send email too frequent");
  }

  const transporter = nodemailer.createTransport({
    host: "mail.sekai.best",
    port: 587,
    secure: false,
    auth: {
      user: process.env.MAIL_USER,
      pass: process.env.MAIL_PASS,
    },
  });

  const targetAddrs = process.env.MAIL_ADDR_RECV_WARN.split(",");
  for (let to of targetAddrs) {
    await transporter.sendMail({
      from: `"Sekai Viewer Warn System" <${process.env.MAIL_ADDR_SEND_WARN}>`,
      to,
      subject: "Failed to connect to pjsk server",
      text,
    });
  }

  lastEmailSent = new Date().getTime();
};

export const checkGitFolder = async function (folderPath, remoteGitBase) {
  try {
    await fsp.stat(folderPath);
  } catch (e) {
    if (e.code === "ENOENT") {
      await git.clone({
        fs,
        http,
        dir: folderPath,
        url: remoteGitBase + "/" + path.basename(folderPath),
      });
      await git.checkout({
        fs,
        dir: folderPath,
        ref: "main",
      });
    }
  }
};

export function merge(source, target) {
  for (const [key, val] of Object.entries(source)) {
    if (val !== null && typeof val === `object`) {
      if (target[key] === undefined) {
        target[key] = new val.__proto__.constructor();
      }
      merge(val, target[key]);
    } else {
      target[key] = val;
    }
  }
  return target; // we're replacing in-situ, so this is more for chaining than anything else
}

export async function clientRequest(client, ...args) {
  const res = await client.request(...args);
  if (res.result) return res.result;
  if (res.error) {
    console.error(...args, res.error);
    throw res;
  }
}
