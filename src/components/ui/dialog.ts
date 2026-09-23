import { reactive } from 'vue'

export type DialogKind = 'alert' | 'confirm' | 'prompt'

export interface DialogOptions {
  title?: string
  confirmText?: string
  cancelText?: string
  destructive?: boolean
  inputLabel?: string
  placeholder?: string
}

export interface DialogRequest {
  id: number
  kind: DialogKind
  title: string
  message: string
  confirmText: string
  cancelText: string
  destructive: boolean
  inputLabel: string
  placeholder: string
  inputValue: string
  resolve: (value: boolean | string | null | undefined) => void
}

export const dialogState = reactive({
  current: null as DialogRequest | null,
  queue: [] as DialogRequest[],
})

let nextDialogId = 0

function openDialog<T>(
  kind: DialogKind,
  message: string,
  options: DialogOptions = {},
  inputValue = '',
): Promise<T> {
  return new Promise<T>((resolve) => {
    const request: DialogRequest = {
      id: ++nextDialogId,
      kind,
      title: options.title || (kind === 'alert' ? '提示' : kind === 'confirm' ? '确认操作' : '输入内容'),
      message,
      confirmText: options.confirmText || (kind === 'alert' ? '知道了' : '确定'),
      cancelText: options.cancelText || '取消',
      destructive: options.destructive ?? false,
      inputLabel: options.inputLabel || '输入内容',
      placeholder: options.placeholder || '',
      inputValue,
      resolve: (value) => resolve(value as T),
    }
    if (dialogState.current) dialogState.queue.push(request)
    else dialogState.current = request
  })
}

export function showAlert(message: string, options: Omit<DialogOptions, 'destructive' | 'inputLabel' | 'placeholder'> = {}): Promise<void> {
  return openDialog<void>('alert', message, options)
}

export function showConfirm(message: string, options: Omit<DialogOptions, 'inputLabel' | 'placeholder'> = {}): Promise<boolean> {
  return openDialog<boolean>('confirm', message, options)
}

export function showPrompt(
  message: string,
  initialValue = '',
  options: Omit<DialogOptions, 'destructive'> = {},
): Promise<string | null> {
  return openDialog<string | null>('prompt', message, options, initialValue)
}

export function settleDialog(value: boolean | string | null | undefined): void {
  const current = dialogState.current
  if (!current) return
  current.resolve(value)
  dialogState.current = dialogState.queue.shift() ?? null
}
