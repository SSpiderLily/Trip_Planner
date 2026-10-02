<template>
  <a-config-provider :locale="zhCN">
    <div id="app">
      <header class="site-header">
        <router-link class="brand" to="/" aria-label="漫游旅行规划首页">
          <span class="brand-mark" aria-hidden="true">✳</span>
          <span>漫游<span class="brand-dot">.</span></span>
        </router-link>
        <div class="header-caption">A JOURNEY, WELL CONSIDERED</div>
        <router-link class="header-link" to="/#plan">开始规划 <span aria-hidden="true">↗</span></router-link>
      </header>
      <div class="page-content"><router-view /></div>
      <footer class="site-footer">
        <span class="footer-brand">漫游<span>.</span></span>
        <span>把远方，变成一段值得记住的日常。</span>
        <span>POWERED BY HELLOAGENTS · © 2026</span>
      </footer>
    </div>
  </a-config-provider>
</template>

<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN'
</script>

<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&family=Noto+Serif+SC:wght@500;600;700&display=swap');

:root { font-family: 'DM Sans', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif; color: #1b2c28; background: #f5f3ed; font-synthesis: none; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; min-width: 320px; }
button, input, textarea { font: inherit; }
button, a { -webkit-tap-highlight-color: transparent; }
a { color: inherit; }
::selection { background: #d7e3b8; color: #1b2c28; }
.site-header { height: 82px; padding: 0 clamp(24px, 5vw, 86px); display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid rgba(27,44,40,.14); background: #f5f3ed; position: relative; z-index: 10; }
.brand { display: inline-flex; align-items: center; gap: 9px; text-decoration: none; font-family: 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', serif; font-size: 27px; font-weight: 700; letter-spacing: -.08em; }
.brand-mark { width: 35px; height: 35px; background: #d7e3b8; color: #1d392e; display: grid; place-items: center; border-radius: 50%; font-family: sans-serif; font-size: 26px; line-height: 1; transition: transform .45s ease; }
.brand:hover .brand-mark { transform: rotate(85deg); }
.brand-dot, .footer-brand span { color: #d46b46; }
.header-caption { font-size: 10px; font-weight: 700; letter-spacing: .23em; opacity: .52; }
.header-link { font-size: 13px; font-weight: 700; text-decoration: none; border-bottom: 1px solid currentColor; padding: 8px 0; transition: color .2s; }
.header-link:hover { color: #ca6b47; }
.header-link span { margin-left: 14px; }
.site-footer { padding: 38px clamp(24px, 5vw, 86px); display: flex; align-items: center; justify-content: space-between; gap: 20px; background: #1b322c; color: #f5f3ed; font-size: 12px; letter-spacing: .04em; }
.footer-brand { font-family: 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', serif; font-size: 28px; font-weight: 700; letter-spacing: -.08em; }
.site-footer > span:last-child { font-size: 10px; opacity: .56; letter-spacing: .14em; }
:focus-visible { outline: 2px solid #d46b46; outline-offset: 3px; }
@media (max-width: 650px) { .site-header { height: 68px; } .header-caption { display: none; } .site-footer { flex-direction: column; text-align: center; padding: 36px 20px; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; transition-duration: .01ms !important; } }
</style>
