const nodemailer = require("nodemailer");

let lastEmailSent = 0;

module.exports.sendEmail = async function () {
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
      text: "The connection to project sekai server failed, please reboot proxy!!!",
    });
  }

  lastEmailSent = new Date().getTime();
};
