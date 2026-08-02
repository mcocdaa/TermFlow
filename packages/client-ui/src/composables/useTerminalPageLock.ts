import { onBeforeUnmount, watch, type Ref } from 'vue'

const CLASS_NAME = 'termflow-terminal-route'

export function useTerminalPageLock(active: Readonly<Ref<boolean>>) {
  let locked = false
  let scrollX = 0
  let scrollY = 0
  const roots = () => [
    document.documentElement,
    document.body,
    document.getElementById('app'),
  ].filter((element): element is HTMLElement => element instanceof HTMLElement)

  function unlock() {
    if (!locked) return
    roots().forEach((element) => element.classList.remove(CLASS_NAME))
    locked = false
    window.scrollTo(scrollX, scrollY)
  }

  watch(active, (enabled) => {
    if (!enabled) {
      unlock()
      return
    }
    if (locked) return
    scrollX = window.scrollX
    scrollY = window.scrollY
    roots().forEach((element) => element.classList.add(CLASS_NAME))
    locked = true
  }, { immediate: true })

  onBeforeUnmount(unlock)
}
