import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { Alert, AutoComplete, Button, ConfigProvider, DatePicker, Empty, Form, Input, InputNumber, Select, Space, Spin, Tree } from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import App from './App.vue'
import Home from './views/Home.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    ...(import.meta.env.DEV ? [{ path: '/prototype/result', name: 'TripResultPrototype', component: () => import('./views/TripResultPrototype.vue') }] : []),
    { path: '/observability', name: 'Observability', component: () => import('./views/Observability.vue') },
    {
      path: '/',
      name: 'Home',
      component: Home
    },
    {
      path: '/result',
      name: 'Result',
      component: () => import('./views/RouteResult.vue')
    }
  ],
  scrollBehavior(to) {
    if (to.hash) return { el: to.hash, behavior: 'smooth' }
    return { top: 0 }
  }
})

dayjs.locale('zh-cn')
const app = createApp(App)

app.use(router)
for (const component of [Alert, AutoComplete, Button, ConfigProvider, DatePicker, Empty, Form, Input, InputNumber, Select, Space, Spin, Tree]) {
  app.use(component)
}

app.mount('#app')
