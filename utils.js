const nodemailer = require("nodemailer");
const fs = require("fs");
const fsp = require("fs/promises");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const path = require("path");

let lastEmailSent = 0;

module.exports.sendEmail = async function (text) {
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

module.exports.checkGitFolder = async function (folderPath, remoteGitBase) {
  try {
    await fsp.stat(folderPath);
  } catch (e) {
    if (e.code === "ENOENT") {
      await git.clone({
        fs,
        http,
        dir: folderPath,
        url: remoteGitBase + '/' + path.basename(folderPath),
      });
      await git.checkout({
        fs,
        dir: folderPath,
        ref: 'main'
      })
    }
  }
};
